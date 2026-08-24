"""
Tests for app/compute.py

Covers:
- Zone success-rate tracking (rolling window, zone ordering, reset, summary).
- Compute-job VM heartbeat endpoint and provisioning-timeout guard.
"""

import asyncio
import io
import json
import tarfile
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, call, patch
import os
import shlex
import signal
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

from app.time_utils import _utcnow
from app import compute
from app import compute_backends as compute_backends_module
from app import compute_jobs
from app import provision_error_scope
from app import storage
from app.managed_compute import (
    ManagedComputeDisabled,
    is_managed_compute_enabled_authoritative,
)
from app.compute_backends import (
    ComputeCostResult,
    GcpComputeBackend,
    ComputeLaunchResult,
    ComputeLaunchSpec,
    ComputeLaunchSpecError,
    LocalSubprocessComputeBackend,
    ComputeStatusResult,
    RunnerSubprocessBackend,
    RUNNER_SUBPROCESS_BACKEND_NAME,
    TerminalArtifactReference,
    OPERATOR_HARDWARE_NOMINAL_RATE_PER_SECOND,
)
from app import auth as auth_mod
from app.database import resolve_async_engine
from testing._compute_test_helpers import (
    _make_compute_task,
    _make_tar_gz_bundle,
    _terminal_record_bytes,
)
from app.models import (
    Artifact,
    ArtifactType,
    ComputeJob,
    ComputeJobOutcome,
    ComputeJobStatus,
    DeploymentState,
    KnowledgeDocument,
    LocalRunner,
    Project,
    ReleaseDeployArtifact,
    ReleaseVersion,
    RunnerProjectBinding,
    Task,
    TaskHistory,
    TaskStatus,
    TaskWakeRegistration,
    WakeAction,
    WakeDeliveryState,
)


@pytest.mark.asyncio
async def test_managed_compute_disabled_blocks_gce_agent_before_primitive(monkeypatch):
    """The public agent entrypoint must not reach the GCE primitive when off."""
    from app.runtime_config_state import runtime_config

    monkeypatch.setitem(runtime_config, "managed_compute_enabled", False)
    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    image_check = MagicMock()
    provision = AsyncMock()
    monkeypatch.setattr(compute, "_ensure_image_family_valid", image_check)
    monkeypatch.setattr(compute, "_provision_gcp", provision)

    with pytest.raises(ManagedComputeDisabled, match="managed compute is disabled"):
        await compute.provision_agent_vm(
            "agent-vm-managed-gate",
            uuid.uuid4(),
            task_type="research",
        )

    image_check.assert_called_once()
    provision.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_compute_disabled_blocks_gcp_backend_before_vm_primitive(
    monkeypatch,
):
    """Direct ComputeJob GCP backend callers cannot bypass the cost boundary."""
    from app.runtime_config_state import runtime_config

    monkeypatch.setitem(runtime_config, "managed_compute_enabled", False)
    provision = AsyncMock()
    monkeypatch.setattr(compute, "provision_ephemeral_vm", provision)
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="echo blocked",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
    )

    with pytest.raises(ManagedComputeDisabled, match="managed compute is disabled"):
        await GcpComputeBackend().launch(spec)

    provision.assert_not_awaited()


@pytest.mark.asyncio
async def test_authoritative_managed_compute_gate_overrides_stale_process_cache(
    db_session, monkeypatch
):
    """A second Cloud Run instance's stale cache cannot defeat a DB disable."""
    from app.runtime_config_state import runtime_config

    state = await db_session.get(DeploymentState, 1)
    if state is None:
        state = DeploymentState(id=1)
        db_session.add(state)
    state.runtime_config_json = {"managed_compute_enabled": False}
    db_session.add(state)
    await db_session.commit()
    monkeypatch.setitem(runtime_config, "managed_compute_enabled", True)

    assert await is_managed_compute_enabled_authoritative(db_session) is False


@pytest.mark.asyncio
async def test_project_placement_fence_fails_closed_when_production_db_read_fails(
    monkeypatch,
):
    """An unreadable project policy cannot permit a paid provisioning path."""
    from app import database
    from app.orchestrator_provision import _project_placement_fence

    class _BrokenSession:
        async def __aenter__(self):
            raise RuntimeError("database unavailable")

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class _BrokenFactory:
        def __call__(self):
            return _BrokenSession()

    monkeypatch.setattr(database, "DATABASE_URL", "postgresql+asyncpg://db/blueprint")

    assert await _project_placement_fence(_BrokenFactory(), uuid.uuid4()) == (True, True)


@pytest.mark.asyncio
async def test_project_placement_fence_preserves_sqlite_test_behavior_on_read_failure(
    monkeypatch,
):
    from app import database
    from app.orchestrator_provision import _project_placement_fence

    class _BrokenSession:
        async def __aenter__(self):
            raise RuntimeError("fixture database unavailable")

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class _BrokenFactory:
        def __call__(self):
            return _BrokenSession()

    monkeypatch.setattr(database, "DATABASE_URL", "sqlite+aiosqlite://")

    assert await _project_placement_fence(_BrokenFactory(), uuid.uuid4()) == (False, True)


@pytest.mark.asyncio
async def test_local_only_policy_returns_reserved_gcp_compute_job_to_queued(
    db_session,
):
    """A policy change after STARTING must still prevent the paid launch."""
    project = Project(
        name="Compute placement race",
        user_id="test-user",
        build_config={
            "resource_envelope": {"execution_placement_policy": "local_only"}
        },
    )
    task = Task(project_id=project.id, status=TaskStatus.QUEUED)
    db_session.add_all([project, task])
    await db_session.flush()
    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend="gcp",
        status=ComputeJobStatus.STARTING,
        started_at=_utcnow(),
    )
    db_session.add(job)
    await db_session.commit()

    session_factory = async_sessionmaker(
        resolve_async_engine(db_session), expire_on_commit=False, class_=AsyncSession
    )
    assert await compute_jobs._compute_job_project_is_local_only(session_factory, job)
    await compute_jobs._hold_compute_job_for_managed_compute(
        session_factory, job.id, now=_utcnow(), local_only=True
    )

    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.QUEUED
    assert job.started_at is None
    assert (job.polling_metadata or {})["execution_placement_wait"]["policy"] == "local_only"


def _make_job_with_command_log(text: str, truncated: bool = False) -> ComputeJob:
    return ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        backend="gcp",
        machine_type="g2-standard-4",
        status=ComputeJobStatus.SUCCEEDED,
        outcome=ComputeJobOutcome.SUCCESS,
        accrued_cost_usd=0.5,
        final_cost_usd=0.5,
        polling_metadata={
            "command_log": {
                "text": text,
                "truncated": truncated,
                "byte_count": len(text.encode()),
            },
        },
    )


@pytest_asyncio.fixture()
async def gcp_cleanup_db(mocker):
    """Isolated DB for _cleanup_gcp revalidation tests."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    session_factory = sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    mocker.patch("app.compute.AsyncSessionLocal", session_factory)

    async with session_factory() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


class _FleetSourceFakeSession:
    async def execute(self, _stmt):
        raise RuntimeError("db unavailable")


@pytest.mark.asyncio
async def test_cleanup_gcp_skips_vm_that_becomes_active_mid_sweep(
    gcp_cleanup_db, mocker
):
    """A VM that flips to IN_PROGRESS after inventory but before deletion must
    be spared by the fresh revalidation step."""
    project = Project(name="Cleanup Revalidate", user_id="user-1")
    gcp_cleanup_db.add(project)
    await gcp_cleanup_db.commit()
    await gcp_cleanup_db.refresh(project)

    target_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "test"}],
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        vm_instance_id="agent-vm-flip-1",
    )
    gcp_cleanup_db.add(target_task)
    await gcp_cleanup_db.commit()

    mocker.patch("app.compute.LOCAL_MODE", False)
    mocker.patch("app.compute.FALLBACK_ZONES", [("us-central1-a", "subnet-a")])
    mocker.patch("app.compute.GPU_FALLBACK_ZONES", {})
    mocker.patch(
        "app.compute._list_vms_in_zone",
        return_value=[
            ("agent-vm-flip-1", "us-central1-a", "RUNNING"),
            ("agent-vm-orphan-1", "us-central1-a", "RUNNING"),
        ],
    )
    delete_mock = mocker.patch("app.compute._delete_single_vm", return_value=True)

    from app.services import _collect_gcp_cleanup_active_vm_ids as real_collect

    flipped = {"done": False}

    async def _flip_then_collect(session):
        if not flipped["done"]:
            target_task.status = TaskStatus.IN_PROGRESS
            gcp_cleanup_db.add(target_task)
            await gcp_cleanup_db.commit()
            flipped["done"] = True
        return await real_collect(session)

    mocker.patch(
        "app.services._collect_gcp_cleanup_active_vm_ids",
        side_effect=_flip_then_collect,
    )

    result = await compute._cleanup_gcp([])

    assert "agent-vm-orphan-1" in result.deleted
    assert "agent-vm-flip-1" not in result.deleted
    assert delete_mock.call_args_list == [call("agent-vm-orphan-1", "us-central1-a")]


@pytest.mark.asyncio
async def test_gpu_vm_lifecycle_searches_gpu_zone_ring_after_restart(monkeypatch):
    """GPU-only zones (e.g. us-east1-d, us-east4-c) must be searched by
    find_vm_zone, stop_vm, _delete_gcp, and _cleanup_gcp so compute-job VMs
    provisioned in accelerator-specific zones survive process restarts and are
    included in orphan cleanup."""
    # us-east1-d and us-east4-c are in GPU_FALLBACK_ZONES but NOT in FALLBACK_ZONES.
    cpu_only_zones = [
        ("us-central1-f", "agent-sandbox-subnet"),
        ("us-east1-b", "agent-sandbox-subnet-us-east1"),
    ]
    gpu_only_zones = {
        "nvidia-tesla-t4": [
            ("us-east1-d", "agent-sandbox-subnet-us-east1"),
            ("us-east4-c", "agent-sandbox-subnet-us-east4"),
        ]
    }
    monkeypatch.setattr(compute, "FALLBACK_ZONES", cpu_only_zones)
    monkeypatch.setattr(compute, "GPU_FALLBACK_ZONES", gpu_only_zones)
    monkeypatch.setattr(compute, "_vm_zones", {})

    # --- find_vm_zone discovers a GPU-only zone after restart ---
    probed: list[str] = []

    def fake_get_vm_status(vm_name: str, zone: str) -> str | None:
        probed.append(zone)
        if zone == "us-east1-d" and vm_name == "compute-job-vm-gpu-1":
            return "RUNNING"
        return "DELETED"

    monkeypatch.setattr(compute, "get_vm_status", fake_get_vm_status)
    result_zone = compute.find_vm_zone("compute-job-vm-gpu-1")
    assert result_zone == "us-east1-d", f"expected us-east1-d, got {result_zone}"
    # CPU zones searched first, then GPU-only zones
    assert "us-central1-f" in probed
    assert "us-east1-d" in probed
    # Zone repopulated in cache
    assert compute._vm_zones.get("compute-job-vm-gpu-1") == "us-east1-d"

    # --- _all_managed_zones includes GPU-only zones but not duplicates ---
    all_zones = compute._all_managed_zones()
    assert "us-east1-d" in all_zones
    assert "us-east4-c" in all_zones
    # CPU zones come first
    assert all_zones.index("us-central1-f") < all_zones.index("us-east1-d")
    # No duplicates
    assert len(all_zones) == len(set(all_zones))

    # --- _cleanup_gcp inventories GPU-only zones ---
    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    listed_zones: list[str] = []

    def fake_list_vms(zone: str):
        listed_zones.append(zone)
        if zone == "us-east4-c":
            return [("compute-job-vm-orphan-gpu", zone, "RUNNING")]
        return []

    monkeypatch.setattr(compute, "_list_vms_in_zone", fake_list_vms)
    deleted: list[str] = []

    async def fake_refresh_active():
        return set()

    monkeypatch.setattr(
        compute, "_refresh_gcp_cleanup_active_vm_ids", fake_refresh_active
    )

    def fake_delete_single_vm(name, zone):
        deleted.append(name)
        return True

    def fake_delete_agent_secret_sync(name):
        pass

    monkeypatch.setattr(compute, "_delete_single_vm", fake_delete_single_vm)
    monkeypatch.setattr(compute, "_delete_agent_secret", fake_delete_agent_secret_sync)

    cleanup_result = await compute._cleanup_gcp([])
    assert "us-east4-c" in listed_zones, "cleanup must scan GPU-only zone us-east4-c"
    assert "compute-job-vm-orphan-gpu" in cleanup_result.deleted


@pytest.mark.asyncio
async def test_fleet_serving_source_sha_preserves_exact_match_without_ancestry(
    monkeypatch, mocker
):
    monkeypatch.setattr(compute, "_vm_source_shas", {"vm-1": "b" * 40})
    ancestry = mocker.patch(
        "app.github_refs.is_ancestor_of",
        new=AsyncMock(return_value=True),
    )

    # Without a project scope, the transient heartbeat cache is admissible.
    assert (
        await compute.fleet_serving_source_sha(
            _FleetSourceFakeSession(),
            "b" * 40,
        )
        is True
    )
    ancestry.assert_not_awaited()


@pytest.mark.asyncio
async def test_fleet_serving_source_sha_accepts_live_local_runner(db_session):
    """A live checkout runner can independently prove the promoted source SHA."""
    promoted_sha = "a" * 40
    project = Project(name="Local serving identity", user_id="test-user")
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        LocalRunner(
            project_id=project.id,
            token_hash="0" * 64,
            status="active",
            last_seen_at=_utcnow(),
            source_sha=promoted_sha,
        )
    )
    await db_session.commit()

    assert (
        await compute.fleet_serving_source_sha(
            db_session,
            promoted_sha,
            project_id=project.id,
        )
        is True
    )


@pytest.mark.asyncio
async def test_fleet_serving_source_sha_rejects_local_runner_on_different_sha(
    db_session,
):
    """A checkout runner reporting another commit must not satisfy the gate."""
    promoted_sha = "a" * 40
    project = Project(name="Wrong local serving identity", user_id="test-user")
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        LocalRunner(
            project_id=project.id,
            token_hash="1" * 64,
            status="active",
            last_seen_at=_utcnow(),
            source_sha="b" * 40,
        )
    )
    await db_session.commit()

    assert (
        await compute.fleet_serving_source_sha(
            db_session,
            promoted_sha,
            project_id=project.id,
        )
        is False
    )


@pytest.mark.asyncio
async def test_fleet_serving_source_sha_ignores_legacy_abbreviated_runner_sha(
    db_session,
):
    """A legacy short runner identity is unobservable, not a serving mismatch."""
    promoted_sha = "a" * 40
    project = Project(name="Legacy runner identity", user_id="test-user")
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        LocalRunner(
            project_id=project.id,
            token_hash="2" * 64,
            status="active",
            last_seen_at=_utcnow(),
            source_sha=promoted_sha[:7],
        )
    )
    await db_session.commit()

    assert (
        await compute.fleet_serving_source_sha(
            db_session,
            promoted_sha,
            project_id=project.id,
        )
        is None
    )


@pytest.mark.asyncio
async def test_fleet_serving_source_sha_accepts_descendant_when_enabled(
    monkeypatch, mocker
):
    promoted_sha = "a" * 40
    descendant_sha = "b" * 40
    monkeypatch.setattr(compute, "_vm_source_shas", {"vm-1": descendant_sha})
    ancestry = mocker.patch(
        "app.github_refs.is_ancestor_of",
        new=AsyncMock(return_value=True),
    )

    assert (
        await compute.fleet_serving_source_sha(
            _FleetSourceFakeSession(),
            promoted_sha,
            allow_descendant=True,
            token="tok",
            owner="acme",
            repo="widget",
        )
        is True
    )
    ancestry.assert_awaited_once()
    assert ancestry.await_args.args[3:5] == (promoted_sha, descendant_sha)


@pytest.mark.asyncio
async def test_fleet_serving_source_sha_indeterminate_descendant_is_not_confirmed(
    monkeypatch, mocker
):
    monkeypatch.setattr(compute, "_vm_source_shas", {"vm-1": "b" * 40})
    mocker.patch(
        "app.github_refs.is_ancestor_of",
        new=AsyncMock(return_value=None),
    )

    assert (
        await compute.fleet_serving_source_sha(
            _FleetSourceFakeSession(),
            "a" * 40,
            allow_descendant=True,
            token="tok",
            owner="acme",
            repo="widget",
        )
        is False
    )


@pytest.mark.asyncio
async def test_fleet_serving_source_sha_confirms_from_live_local_runner(
    gcp_cleanup_db,
):
    """A self-hosted checkout runner with a durable agent_source_sha confirms
    fleet serving.

    The fleet-serving predicate behind the agent_image release artifact only
    inspected warm-pool PoolVm rows and a task-bound in-memory cache, so a fleet
    made entirely of durable operator-hardware runners could never satisfy it.
    This exercises the real DB path (no in-memory heartbeat cache populated) for
    a fleet that is entirely LocalRunner rows — exactly the release gate's
    project-scoped call shape.
    """
    from app.models import LocalRunner
    from app.time_utils import _utcnow

    promoted_sha = "c" * 40
    project = Project(name="Runner Fleet Serving", user_id="user-1")
    gcp_cleanup_db.add(project)
    await gcp_cleanup_db.commit()
    await gcp_cleanup_db.refresh(project)

    runner = LocalRunner(
        project_id=project.id,
        token_hash="a" * 64,
        hostname="operator-box-1",
        version="v1.2.3-5-g" + promoted_sha[:7],
        agent_source_sha=promoted_sha,
        status="active",
        last_seen_at=_utcnow(),
    )
    gcp_cleanup_db.add(runner)
    await gcp_cleanup_db.commit()

    assert (
        await compute.fleet_serving_source_sha(
            gcp_cleanup_db,
            promoted_sha,
            allow_descendant=True,
            token="tok",
            owner="acme",
            repo="widget",
            project_id=str(project.id),
        )
        is True
    )


@pytest.mark.asyncio
async def test_fleet_serving_source_sha_ignores_inactive_and_other_project_runner(
    gcp_cleanup_db,
):
    """Only active, live, project-scoped runners with a reported SHA confirm."""
    from app.models import LocalRunner
    from app.time_utils import _utcnow

    promoted_sha = "c" * 40
    project = Project(name="Runner Fleet Scoped", user_id="user-1")
    gcp_cleanup_db.add(project)
    await gcp_cleanup_db.commit()
    await gcp_cleanup_db.refresh(project)
    other_project = Project(name="Other Project", user_id="user-2")
    gcp_cleanup_db.add(other_project)
    await gcp_cleanup_db.commit()
    await gcp_cleanup_db.refresh(other_project)

    gcp_cleanup_db.add_all(
        [
            # Inactive runner on the right project.
            LocalRunner(
                project_id=project.id,
                token_hash="b" * 64,
                status="inactive",
                agent_source_sha=promoted_sha,
                last_seen_at=_utcnow(),
            ),
            # Active but stale (missed liveness window) runner on the right project.
            LocalRunner(
                project_id=project.id,
                token_hash="c" * 64,
                status="active",
                agent_source_sha=promoted_sha,
                last_seen_at=_utcnow() - timedelta(seconds=10000),
            ),
            # Active + live runner on a DIFFERENT project (tenant isolation).
            LocalRunner(
                project_id=other_project.id,
                token_hash="d" * 64,
                status="active",
                agent_source_sha=promoted_sha,
                last_seen_at=_utcnow(),
            ),
        ]
    )
    await gcp_cleanup_db.commit()

    assert (
        await compute.fleet_serving_source_sha(
            gcp_cleanup_db,
            promoted_sha,
            project_id=str(project.id),
        )
        is None
    )


@pytest.mark.asyncio
async def test_bound_runner_is_visible_to_serving_gate_and_snapshot(gcp_cleanup_db):
    """A live runner explicitly bound to a project appears in both fleet paths."""
    promoted_sha = "b" * 40
    project = Project(name="Bound runner target", user_id="user-1")
    enrollment_project = Project(name="Bound runner enrollment", user_id="user-2")
    gcp_cleanup_db.add_all([project, enrollment_project])
    await gcp_cleanup_db.commit()
    await gcp_cleanup_db.refresh(project)
    await gcp_cleanup_db.refresh(enrollment_project)

    runner = LocalRunner(
        project_id=enrollment_project.id,
        token_hash="f" * 64,
        status="active",
        last_seen_at=_utcnow(),
        agent_source_sha=promoted_sha,
    )
    gcp_cleanup_db.add(runner)
    await gcp_cleanup_db.commit()
    await gcp_cleanup_db.refresh(runner)
    gcp_cleanup_db.add(
        RunnerProjectBinding(runner_id=runner.id, project_id=project.id)
    )
    await gcp_cleanup_db.commit()

    assert (
        await compute.fleet_serving_source_sha(
            gcp_cleanup_db,
            promoted_sha,
            project_id=str(project.id),
        )
        is True
    )

    snapshot = await compute.agent_fleet_source_sha_snapshot(
        gcp_cleanup_db, project.id
    )
    assert snapshot["total_observed"] == 1
    assert snapshot["buckets"][0]["source_sha"] == promoted_sha
    assert snapshot["buckets"][0]["runner_count"] == 1


@pytest.mark.asyncio
async def test_agent_fleet_source_sha_snapshot_falls_back_to_runner_source_sha(
    gcp_cleanup_db,
):
    """The snapshot exposes the runner identity when heartbeat SHA is absent."""
    project = Project(name="Runner source fallback", user_id="user-1")
    gcp_cleanup_db.add(project)
    await gcp_cleanup_db.commit()
    await gcp_cleanup_db.refresh(project)

    source_sha = "d" * 40
    gcp_cleanup_db.add(
        LocalRunner(
            project_id=project.id,
            token_hash="1" * 64,
            status="active",
            last_seen_at=_utcnow(),
            source_sha=source_sha,
        )
    )
    await gcp_cleanup_db.commit()

    snapshot = await compute.agent_fleet_source_sha_snapshot(
        gcp_cleanup_db, project.id
    )
    assert snapshot["unknown_count"] == 0
    assert snapshot["buckets"][0]["source_sha"] == source_sha
    assert snapshot["buckets"][0]["runner_count"] == 1


@pytest.mark.asyncio
async def test_agent_fleet_source_sha_snapshot_includes_runner_kind(gcp_cleanup_db):
    """Live LocalRunner rows appear as runner-kind buckets in the snapshot."""
    from app.models import LocalRunner
    from app.time_utils import _utcnow

    project = Project(name="Runner Snapshot", user_id="user-1")
    gcp_cleanup_db.add(project)
    await gcp_cleanup_db.commit()
    await gcp_cleanup_db.refresh(project)

    runner = LocalRunner(
        project_id=project.id,
        token_hash="e" * 64,
        status="active",
        agent_source_sha="f" * 40,
        last_seen_at=_utcnow(),
    )
    gcp_cleanup_db.add(runner)
    await gcp_cleanup_db.commit()

    snapshot = await compute.agent_fleet_source_sha_snapshot(
        gcp_cleanup_db, project.id
    )
    assert snapshot["total_observed"] == 1
    assert snapshot["mixed"] is False
    assert snapshot["unknown_count"] == 0
    bucket = snapshot["buckets"][0]
    assert bucket["source_sha"] == "f" * 40
    assert bucket["runner_count"] == 1
    assert bucket["count"] == 1
    assert bucket["warm_pool_count"] == 0
    assert bucket["task_vm_count"] == 0


async def test_local_compute_backend_recovers_running_pid_after_restart():
    backend = LocalSubprocessComputeBackend()
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="sleep 30",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
    )
    result = await backend.launch(spec)
    pid = result.metadata["pid"]
    compute_job = ComputeJob(
        id=spec.job_id,
        project_id=uuid.uuid4(),
        backend=backend.name,
        external_compute_id=result.external_id,
        status=ComputeJobStatus.RUNNING,
        polling_metadata={"pid": pid},
    )

    try:
        backend._processes.clear()
        status = await backend.poll_status(compute_job)
        assert status.status == ComputeJobStatus.RUNNING
    finally:
        os.killpg(pid, signal.SIGTERM)


@pytest.mark.asyncio
async def test_local_compute_backend_recovers_completed_success_after_restart():
    backend = LocalSubprocessComputeBackend()
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="exit 0",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
    )
    result = await backend.launch(spec)
    pid = result.metadata["pid"]
    compute_job = ComputeJob(
        id=spec.job_id,
        project_id=uuid.uuid4(),
        backend=backend.name,
        external_compute_id=result.external_id,
        status=ComputeJobStatus.RUNNING,
        polling_metadata={"pid": pid},
    )

    for _ in range(40):
        if compute_backends_module._read_local_terminal_status(compute_job) is not None:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("expected local terminal marker for completed subprocess")

    backend._processes.clear()
    status = await backend.poll_status(compute_job)

    assert status.status == ComputeJobStatus.SUCCEEDED
    assert status.exit_code == 0
    assert status.message == "local subprocess exited 0"
    assert status.finished_at is not None


@pytest.mark.asyncio
async def test_local_compute_backend_terminates_persisted_pid_after_restart():
    backend = LocalSubprocessComputeBackend()
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="sleep 30",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
    )
    result = await backend.launch(spec)
    pid = result.metadata["pid"]
    compute_job = ComputeJob(
        id=spec.job_id,
        project_id=uuid.uuid4(),
        backend=backend.name,
        external_compute_id=result.external_id,
        status=ComputeJobStatus.RUNNING,
        polling_metadata={"pid": pid},
    )

    backend._processes.clear()
    assert await backend.terminate(compute_job, reason="budget") is True
    for _ in range(20):
        if not compute_backends_module._is_pid_running(pid):
            break
        await asyncio.sleep(0.05)
    else:
        os.killpg(pid, signal.SIGKILL)
        raise AssertionError("terminate should kill the persisted local subprocess pid")


@pytest.mark.asyncio
async def test_local_compute_backend_idempotent_launch_preserves_started_at():
    backend = LocalSubprocessComputeBackend()
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="sleep 30",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
    )
    first = await backend.launch(spec)
    second = await backend.launch(
        ComputeLaunchSpec(
            job_id=spec.job_id,
            command=spec.command,
            machine_type=spec.machine_type,
            spend_cap_usd=spec.spend_cap_usd,
            deadline_at=spec.deadline_at,
            external_id=first.external_id,
        )
    )

    try:
        assert second.external_id == first.external_id
        assert second.started_at == first.started_at
    finally:
        await backend.terminate(
            ComputeJob(
                id=spec.job_id,
                project_id=uuid.uuid4(),
                backend=backend.name,
                external_compute_id=first.external_id,
                status=ComputeJobStatus.RUNNING,
                polling_metadata={"pid": first.metadata["pid"]},
            ),
            reason="cleanup",
        )


@pytest.mark.asyncio
async def test_local_subprocess_env_does_not_inherit_orchestrator_secrets(
    monkeypatch,
):
    for key in (
        "ANTHROPIC_API_KEY",
        "DATABASE_URL",
        "GITHUB_PRIVATE_KEY",
        "AGENT_JWT_SECRET",
        "WS_TICKET_SECRET",
    ):
        monkeypatch.setenv(key, f"sentinel-{key}")

    fake_proc = MagicMock()
    fake_proc.pid = 12345
    fake_proc.poll.return_value = None

    backend = LocalSubprocessComputeBackend()
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="env",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        env={"EXPLICIT_JOB_ENV": "present"},
    )

    with patch(
        "app.compute_backends.subprocess.Popen", return_value=fake_proc
    ) as popen:
        result = await backend.launch(spec)

    try:
        child_env = popen.call_args.kwargs["env"]
        assert child_env["EXPLICIT_JOB_ENV"] == "present"
        for key in (
            "ANTHROPIC_API_KEY",
            "DATABASE_URL",
            "GITHUB_PRIVATE_KEY",
            "AGENT_JWT_SECRET",
            "WS_TICKET_SECRET",
        ):
            assert key not in child_env
    finally:
        backend._processes.pop(result.external_id, None)


@pytest.mark.asyncio
async def test_gcp_compute_backend_uses_persisted_zone_for_restart_serial_recovery(
    monkeypatch,
):
    backend = GcpComputeBackend()
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend=backend.name,
        external_compute_id="compute-job-vm-abcd1234",
        status=ComputeJobStatus.RUNNING,
        polling_metadata={"zone": "us-central1-f"},
    )

    def fake_get_vm_serial_output(vm_instance_id: str, zone: str | None = None) -> str:
        assert vm_instance_id == "compute-job-vm-abcd1234"
        assert zone == "us-central1-f"
        return "\n".join(
            [
                "noise",
                "BLUEPRINT_COMPUTE_REASON=completed",
                "BLUEPRINT_COMPUTE_STATUS=SUCCEEDED",
                "BLUEPRINT_COMPUTE_EXIT_CODE=0",
            ]
        )

    def fail_get_vm_status(*args, **kwargs):
        raise AssertionError(
            "serial marker should finalize the job before VM status lookup"
        )

    monkeypatch.setattr(compute, "get_vm_serial_output", fake_get_vm_serial_output)
    monkeypatch.setattr(compute, "get_vm_status", fail_get_vm_status)

    status = await backend.poll_status(job)

    assert status.status == ComputeJobStatus.SUCCEEDED
    assert status.exit_code == 0
    assert status.message == "completed"


@pytest.mark.asyncio
async def test_gcp_compute_backend_adopts_existing_vm_with_original_started_at(
    monkeypatch,
):
    backend = GcpComputeBackend()
    started_at = datetime(2026, 1, 2, 3, 4, 5)

    monkeypatch.setattr(compute, "find_vm_zone", lambda _vm_name: "us-central1-f")
    monkeypatch.setattr(compute, "get_vm_created_at", lambda *_args: started_at)

    result = await backend.launch(
        ComputeLaunchSpec(
            job_id=uuid.uuid4(),
            command="echo ok",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_at=None,
        )
    )

    assert result.started_at == started_at
    assert result.metadata["zone"] == "us-central1-f"


@pytest.mark.asyncio
async def test_gcp_compute_backend_launch_stamps_heartbeat_auth_marker(monkeypatch):
    """GcpComputeBackend.launch() must stamp heartbeat_auth='hmac' in returned metadata.

    This marker is persisted to polling_metadata via _apply_launch_result, which
    is how the heartbeat endpoint knows this VM's startup script embeds the HMAC
    token (and therefore the legacy UUID bearer must be rejected).
    """
    backend = GcpComputeBackend()
    job_id = uuid.uuid4()
    spec = ComputeLaunchSpec(
        job_id=job_id,
        command="python3 train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
    )

    async def fake_signed_url(jid, *, expires_at=None):
        return "https://storage.example.com/upload"

    async def fake_provision(vm_id, **kwargs):
        return True

    monkeypatch.setattr(
        storage, "generate_compute_job_artifact_upload_signed_url", fake_signed_url
    )
    monkeypatch.setattr(compute, "provision_ephemeral_vm", fake_provision)
    monkeypatch.setattr(compute, "find_vm_zone", lambda _vm_name: None)
    monkeypatch.setattr(compute, "get_vm_zone", lambda _vm_name: "us-central1-f")

    result = await backend.launch(spec)

    assert result.metadata.get("heartbeat_auth") == "hmac", (
        "GCP launch must stamp heartbeat_auth='hmac' so the heartbeat endpoint "
        "can reject legacy UUID bearers for this job"
    )


@pytest.mark.asyncio
async def test_gpu_compute_job_builds_accelerator_instance_config(
    monkeypatch, clean_zone_health
):
    """A GPU compute-job provision builds a GCE instance config with the GPU
    machine type, an attached guest accelerator (correct zonal type + count=1),
    the driver-preinstalled GPU image, ``on_host_maintenance=TERMINATE``, and a
    zone drawn from the GPU-availability ring — NOT the plain fleet ring.

    Exercises the production ``compute._provision_gcp`` GPU branch end-to-end with
    a fake GCE client capturing the built ``Instance`` proto (no real GCP calls).
    """
    from google.cloud import compute_v1

    # Force GPU zone selection to a single candidate so the insert zone is
    # deterministic; the candidate is returned by gpu_zone_candidates, proving
    # _provision_gcp consults the GPU ring rather than the fleet ring.
    gpu_zone = "us-east4-c"
    monkeypatch.setattr(
        compute,
        "gpu_zone_candidates",
        lambda accel: [(gpu_zone, "agent-sandbox-subnet-us-east4")],
    )

    def _fail_fleet():
        raise AssertionError("GPU job must not draw zones from the fleet ring")

    monkeypatch.setattr(compute, "_active_fallback_zones", _fail_fleet)

    async def _ok_slot(_vm):
        return True

    async def _noop_release(_vm):
        return None

    monkeypatch.setattr(compute, "_acquire_provision_slot", _ok_slot)
    monkeypatch.setattr(compute, "_release_provision_slot", _noop_release)

    captured: dict = {}

    class _FakeOp:
        name = "op-gpu-1"

        def result(self, timeout=None):
            return None

    class _FakeInstancesClient:
        def get(self, project, zone, instance):
            raise Exception("was not found: 404")

        def insert(self, project, zone, instance_resource):
            captured["zone"] = zone
            captured["instance"] = instance_resource
            return _FakeOp()

        def delete(self, project, zone, instance):
            return _FakeOp()

    monkeypatch.setattr(compute_v1, "InstancesClient", lambda: _FakeInstancesClient())

    ok = await compute._provision_gcp(
        vm_instance_id="compute-job-vm-gputest",
        task_id=None,
        startup_script_override="#!/bin/bash\ntrue\n",
        machine_type="n1-standard-1",
        accelerator="T4",
        accelerator_count=1,
    )

    assert ok is True
    # Zone drawn from the GPU-availability ring.
    assert captured["zone"] == gpu_zone
    instance = captured["instance"]
    # GPU machine type.
    assert instance.machine_type == f"zones/{gpu_zone}/machineTypes/n1-standard-1"
    # Attached guest accelerator: correct zonal type URL + count=1.
    assert len(instance.guest_accelerators) == 1
    accel = instance.guest_accelerators[0]
    assert accel.accelerator_type == (
        f"projects/{compute.GCP_PROJECT_ID}/zones/{gpu_zone}"
        f"/acceleratorTypes/nvidia-tesla-t4"
    )
    assert accel.accelerator_count == 1
    # Driver-preinstalled GPU image family, not the agent image.
    src_image = instance.disks[0].initialize_params.source_image
    assert src_image == compute.GPU_IMAGE_FAMILY
    assert src_image != compute.AGENT_IMAGE_FAMILY
    # GPUs cannot live-migrate.
    assert instance.scheduling.on_host_maintenance == "TERMINATE"
    # STANDARD GPU VM keeps automatic_restart.
    assert instance.scheduling.automatic_restart is True


# ─── GPU provision isolation tests ──────────────────────────────────────────


def _make_gpu_failure_provision_monkeypatch(monkeypatch, error_msg: str):
    """Set up monkeypatches for a GPU provision that always fails with error_msg.

    Returns (acquire_called, insert_called) mutable lists so callers can assert
    that the provision actually ran and hit the expected code path.
    """
    from google.cloud import compute_v1

    gpu_zone = "us-east4-c"
    monkeypatch.setattr(
        compute,
        "gpu_zone_candidates",
        lambda accel: [(gpu_zone, "agent-sandbox-subnet-us-east4")],
    )
    monkeypatch.setattr(
        compute,
        "_active_fallback_zones",
        lambda: [(gpu_zone, "agent-sandbox-subnet-us-east4")],
    )

    async def _ok_slot(_vm):
        return True

    async def _noop_release(_vm):
        return None

    monkeypatch.setattr(compute, "_acquire_provision_slot", _ok_slot)
    monkeypatch.setattr(compute, "_release_provision_slot", _noop_release)
    # On any provision failure, _provision_gcp's cleanup path calls
    # _delete_agent_secret, which constructs a real SecretManagerServiceClient
    # (needs GCP ADC, absent in CI) → DefaultCredentialsError before the test
    # ever reaches its isolation assertion. Stub it, matching the existing
    # provision-failure tests elsewhere in this file.
    monkeypatch.setattr(compute, "_delete_agent_secret", lambda vm_id: None)

    class _FailingInstancesClient:
        def get(self, project, zone, instance):
            raise Exception("was not found: 404")

        def insert(self, project, zone, instance_resource):
            raise Exception(error_msg)

        def delete(self, project, zone, instance):
            class _FakeOp:
                def result(self, timeout=None):
                    return None

            return _FakeOp()

    monkeypatch.setattr(
        compute_v1, "InstancesClient", lambda: _FailingInstancesClient()
    )


@pytest.mark.asyncio
async def test_gpu_provision_failure_does_not_block_cpu_fleet(
    monkeypatch, clean_zone_health
):
    """GPU provisioning failures (zone stockout and GPUS_ALL_REGIONS) must NOT
    arm or modify the CPU fleet's zone health or global quota cooldown state.

    This is the primary acceptance evidence for the GPU/CPU isolation work:
    injecting each GPU failure mode leaves the CPU fleet's zone selection and
    global-quota gate completely unaffected. Proven by asserting that CPU
    state dicts are empty after a GPU failure, and GPU state dicts carry the
    failure evidence.
    """
    # --- Phase 1: GPU zone stockout (ZONE_RESOURCE_POOL_EXHAUSTED) ---
    _make_gpu_failure_provision_monkeypatch(
        monkeypatch, "ZONE_RESOURCE_POOL_EXHAUSTED: GPU capacity unavailable"
    )

    ok = await compute._provision_gcp(
        vm_instance_id="compute-job-vm-gpu-isolation-test",
        task_id=None,
        startup_script_override="#!/bin/bash\ntrue\n",
        accelerator="T4",
        accelerator_count=1,
    )
    assert ok is False  # provision failed (expected)

    # CPU zone health must be completely clean.
    assert compute._zone_health == {}, "GPU stockout must not write CPU _zone_health"
    assert compute._zone_failures == {}, (
        "GPU stockout must not write CPU _zone_failures"
    )
    assert provision_error_scope._global_quota_state == {}, (
        "GPU stockout must not arm CPU global quota cooldown"
    )
    assert provision_error_scope._region_quota_state == {}, (
        "GPU stockout must not arm CPU region quota cooldown"
    )

    # GPU zone health carries the failure evidence.
    assert len(compute._gpu_zone_health) > 0, (
        "GPU stockout must record evidence in GPU zone health"
    )

    # Clean GPU state for next phase.
    compute._gpu_zone_health.clear()
    compute._gpu_zone_failures.clear()
    provision_error_scope._gpu_global_quota_state.clear()
    provision_error_scope._gpu_region_quota_state.clear()

    # --- Phase 2: GPUS_ALL_REGIONS (project-wide GPU quota exhaustion) ---
    _make_gpu_failure_provision_monkeypatch(
        monkeypatch,
        "Quota 'GPUS_ALL_REGIONS' exceeded. Limit: 1.0 globally.",
    )

    ok = await compute._provision_gcp(
        vm_instance_id="compute-job-vm-gpu-isolation-test-2",
        task_id=None,
        startup_script_override="#!/bin/bash\ntrue\n",
        accelerator="T4",
        accelerator_count=1,
    )
    assert ok is False

    # CPU state must still be clean.
    assert compute._zone_health == {}, (
        "GPUS_ALL_REGIONS must not write CPU _zone_health"
    )
    assert compute._zone_failures == {}, (
        "GPUS_ALL_REGIONS must not write CPU _zone_failures"
    )
    assert provision_error_scope._global_quota_state == {}, (
        "GPUS_ALL_REGIONS must not arm CPU global quota cooldown"
    )
    assert provision_error_scope._region_quota_state == {}, (
        "GPUS_ALL_REGIONS must not arm CPU region quota cooldown"
    )

    # GPU global quota cooldown is armed.
    assert (
        provision_error_scope._gpu_global_quota_state.get("cooldown_expires_at")
        is not None
    ), "GPUS_ALL_REGIONS must arm GPU global quota cooldown"


@pytest.mark.asyncio
async def test_gpu_zone_stockout_does_not_cool_cpu_zones(
    monkeypatch, clean_zone_health
):
    """A GPU zone stockout (ZONE_RESOURCE_POOL_EXHAUSTED) writes only to
    _gpu_zone_health / _gpu_zone_failures, leaving CPU _zone_health untouched."""
    _make_gpu_failure_provision_monkeypatch(
        monkeypatch, "ZONE_RESOURCE_POOL_EXHAUSTED: no GPU capacity in zone"
    )

    await compute._provision_gcp(
        vm_instance_id="compute-job-vm-gpu-zonal-test",
        task_id=None,
        startup_script_override="#!/bin/bash\ntrue\n",
        accelerator="T4",
    )

    assert compute._zone_health == {}, "GPU stockout wrote to CPU _zone_health"
    assert compute._zone_failures == {}, "GPU stockout wrote to CPU _zone_failures"
    assert len(compute._gpu_zone_health) > 0, (
        "GPU stockout did not record GPU zone health"
    )


@pytest.mark.asyncio
async def test_gpus_all_regions_does_not_arm_cpu_global_cooldown(
    monkeypatch, clean_zone_health
):
    """GPUS_ALL_REGIONS exhaustion arms the GPU global cooldown but must not
    touch the CPU fleet's _global_quota_state."""
    _make_gpu_failure_provision_monkeypatch(
        monkeypatch,
        "Quota 'GPUS_ALL_REGIONS' exceeded. Limit: 1.0 globally.",
    )

    await compute._provision_gcp(
        vm_instance_id="compute-job-vm-gpu-global-quota-test",
        task_id=None,
        startup_script_override="#!/bin/bash\ntrue\n",
        accelerator="T4",
    )

    assert provision_error_scope._global_quota_state == {}, (
        "GPUS_ALL_REGIONS armed CPU global quota state"
    )
    assert (
        provision_error_scope._gpu_global_quota_state.get("cooldown_expires_at")
        is not None
    ), "GPUS_ALL_REGIONS did not arm GPU global quota state"


@pytest.mark.asyncio
async def test_cpu_zone_stockout_does_not_cool_gpu_zones(
    monkeypatch, clean_zone_health
):
    """A CPU zone stockout writes only to CPU _zone_health / _zone_failures,
    leaving _gpu_zone_health untouched."""
    from google.cloud import compute_v1

    async def _ok_slot(_vm):
        return True

    async def _noop_release(_vm):
        return None

    monkeypatch.setattr(compute, "_acquire_provision_slot", _ok_slot)
    monkeypatch.setattr(compute, "_release_provision_slot", _noop_release)
    # See _make_gpu_failure_provision_monkeypatch: the provision-failure cleanup
    # path constructs a real SecretManagerServiceClient absent GCP ADC.
    monkeypatch.setattr(compute, "_delete_agent_secret", lambda vm_id: None)

    class _FailingCPUInstancesClient:
        def get(self, project, zone, instance):
            raise Exception("was not found: 404")

        def insert(self, project, zone, instance_resource):
            raise Exception("ZONE_RESOURCE_POOL_EXHAUSTED: no e2 capacity in zone")

        def delete(self, project, zone, instance):
            class _FakeOp:
                def result(self, timeout=None):
                    return None

            return _FakeOp()

    monkeypatch.setattr(
        compute_v1, "InstancesClient", lambda: _FailingCPUInstancesClient()
    )
    monkeypatch.setattr(
        compute,
        "_active_fallback_zones",
        lambda: [("us-central1-f", "agent-sandbox-subnet")],
    )

    await compute._provision_gcp(
        vm_instance_id="agent-vm-cpu-stockout-test",
        task_id=None,
        startup_script_override="#!/bin/bash\ntrue\n",
    )

    assert compute._gpu_zone_health == {}, "CPU stockout wrote to GPU _gpu_zone_health"
    assert compute._gpu_zone_failures == {}, (
        "CPU stockout wrote to GPU _gpu_zone_failures"
    )
    assert len(compute._zone_health) > 0, "CPU stockout did not record CPU zone health"




# ── RunnerSubprocessBackend poll_cost: operator-hardware nominal accrual ──────


@pytest.mark.asyncio
async def test_runner_subprocess_poll_cost_accrues_nominal_operator_rate():
    """A running operator-hardware (self-hosted runner) job accrues a small
    NON-ZERO nominal cost over elapsed runtime — reporting a flat $0 zeroed the
    cost/task denominator and made the spend_cap a no-op on GPU jobs."""
    backend = RunnerSubprocessBackend()
    started = datetime(2026, 7, 11, 0, 0, 0)
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend=backend.name,
        status=ComputeJobStatus.RUNNING,
        started_at=started,
        accrued_cost_usd=0.0,
        polling_metadata={"cost_rate_per_second": 0.0},
    )
    as_of = started + timedelta(seconds=3600)  # 1h elapsed
    cost = await backend.poll_cost(job, as_of=as_of)
    expected = 3600 * OPERATOR_HARDWARE_NOMINAL_RATE_PER_SECOND
    assert cost.total_cost_usd == pytest.approx(expected)
    assert cost.total_cost_usd > 0  # the whole point: no longer silently zero
    assert cost.measured_at == as_of


@pytest.mark.asyncio
async def test_runner_subprocess_poll_cost_is_monotonic_never_below_prior():
    """poll_cost never reports below the cost already persisted on the row (so a
    later poll with an earlier ``as_of`` cannot make accrued cost go backwards)."""
    backend = RunnerSubprocessBackend()
    started = datetime(2026, 7, 11, 0, 0, 0)
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend=backend.name,
        status=ComputeJobStatus.RUNNING,
        started_at=started,
        accrued_cost_usd=5.0,  # already accrued more than a short elapse implies
    )
    cost = await backend.poll_cost(job, as_of=started + timedelta(seconds=10))
    assert cost.total_cost_usd == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_runner_subprocess_poll_cost_no_started_at_returns_prior():
    """Before the job has a start time there is nothing to accrue against; return
    the prior accrued cost rather than fabricating one."""
    backend = RunnerSubprocessBackend()
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend=backend.name,
        status=ComputeJobStatus.STARTING,
        started_at=None,
        accrued_cost_usd=0.0,
    )
    cost = await backend.poll_cost(job, as_of=datetime(2026, 7, 11, 0, 0, 0))
    assert cost.total_cost_usd == 0.0


@pytest.mark.asyncio
async def test_runner_subprocess_poll_cost_bounds_runtime_for_spend_cap():
    """The nominal accrual eventually reaches any positive spend_cap, so the
    monitor's ``accrued_cost_usd >= spend_cap_usd`` termination is reachable
    (spend_cap is no longer a no-op on operator-hardware jobs)."""
    backend = RunnerSubprocessBackend()
    started = datetime(2026, 7, 11, 0, 0, 0)
    spend_cap = 1.0
    seconds_to_cap = spend_cap / OPERATOR_HARDWARE_NOMINAL_RATE_PER_SECOND
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend=backend.name,
        status=ComputeJobStatus.RUNNING,
        started_at=started,
        accrued_cost_usd=0.0,
        spend_cap_usd=spend_cap,
    )
    cost = await backend.poll_cost(
        job, as_of=started + timedelta(seconds=seconds_to_cap + 1)
    )
    assert cost.total_cost_usd >= spend_cap


@pytest.mark.asyncio
async def test_runner_subprocess_poll_status_exit_125_classifies_no_progress():
    """A runner no-progress watchdog kill reports exec_status=failed exit_code=125
    with no reason field (the /report endpoint carries none). poll_status must
    translate exit 125 into a terminal reason string that
    classify_compute_terminal_reason maps to NO_PROGRESS_TIMEOUT — otherwise the
    platform kill collapses to CODE_ERROR and burns retry budget."""
    from app.failure_classification import (
        NO_PROGRESS_TIMEOUT,
        classify_compute_terminal_reason,
    )

    backend = RunnerSubprocessBackend()
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend=backend.name,
        status=ComputeJobStatus.RUNNING,
        polling_metadata={
            "runner_exec_status": "failed",
            "runner_exit_code": 125,
        },
    )
    result = await backend.poll_status(job)
    # Exit 125 is the reserved no-progress watchdog code: poll_status terminalizes
    # it as TIMED_OUT (matching the end-to-end monitor finalize path asserted in
    # test_runner_report_exit_125_finalizes_no_progress_through_monitor), not a
    # generic FAILED — otherwise the platform kill misattributes to the command.
    assert result.status == ComputeJobStatus.TIMED_OUT
    assert result.exit_code == 125
    assert "no_progress_timeout" in result.message
    assert (
        classify_compute_terminal_reason(result.message, None)
        == NO_PROGRESS_TIMEOUT
    )


@pytest.mark.asyncio
async def test_runner_subprocess_poll_status_generic_failure_not_no_progress():
    """A non-125 runner failure keeps the generic terminal reason (does not get
    mislabelled as a no-progress kill)."""
    backend = RunnerSubprocessBackend()
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend=backend.name,
        status=ComputeJobStatus.RUNNING,
        polling_metadata={
            "runner_exec_status": "failed",
            "runner_exit_code": 1,
        },
    )
    result = await backend.poll_status(job)
    assert result.status == ComputeJobStatus.FAILED
    assert result.message == "compute job failed"
    assert "no_progress_timeout" not in result.message


# ── End-to-end: runner /report endpoint → monitor → failure classification ────
#
# The unit tests above cover RunnerSubprocessBackend.poll_status in isolation.
# The three tests below drive the WHOLE runner-job terminal pipeline: a real HTTP
# POST to /api/runner/compute-job/{id}/report (runner Bearer auth), which stamps
# polling_metadata, then process_compute_jobs_once (the monitor) polls that state,
# finalizes the ComputeJob, and the durable terminal_reason is fed through
# classify_compute_terminal_reason. This is the seam the client-side watchdog kill
# (exit 125) and the runtime-deadline kill (exit 124) traverse in production;
# nothing between the report endpoint and the classified outcome is mocked.


async def _make_project_owner(session, *, name):
    """A committed Project. The runner /report endpoint authenticates the runner
    (not a user), and ``allowed_project_ids`` always includes the runner's
    enrollment project, so no ProjectMember row is needed here."""
    project = Project(name=name, user_id="test-user")
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def _make_runner_subprocess_job(
    session, project, runner, *, status=ComputeJobStatus.RUNNING
):
    """A runner-subprocess ComputeJob pinned to ``runner`` and ready for /report.

    The pin (``polling_metadata['runner_target_id']``) is exactly what the report
    endpoint's runner-scoping filter requires, mirroring the claim path in
    /next-compute-job.
    """
    job = ComputeJob(
        project_id=project.id,
        backend=RUNNER_SUBPROCESS_BACKEND_NAME,
        status=status,
        started_at=_utcnow() - timedelta(seconds=30),
        polling_metadata={
            "runner_target_id": str(runner.id),
            "command": "python train.py",
        },
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def _register_runner(session, project):
    token = auth_mod.create_runner_token(uuid.uuid4())
    runner = LocalRunner(
        project_id=project.id,
        token_hash=auth_mod.hash_runner_token(token),
        last_seen_at=_utcnow(),
        capabilities={"task_types": "all", "has_gpu": False, "max_concurrent": 2},
    )
    session.add(runner)
    await session.commit()
    await session.refresh(runner)
    return runner, token


@pytest.mark.asyncio
async def test_stranded_runner_subprocess_sweep_finalizes_only_dead_claimed_jobs(db_session):
    """Dead claimed runners finalize jobs; fresh, unclaimed, and other jobs survive."""
    from app.reaper_sweeps import sweep_stranded_runner_subprocess_compute_jobs
    from app.runner_health_monitors import _RUNNER_LIVENESS_SECONDS

    now = _utcnow()
    project = await _make_project_owner(db_session, name="Stranded runner sweep")
    stale_runner = LocalRunner(project_id=project.id, token_hash="stale", last_seen_at=now - timedelta(seconds=_RUNNER_LIVENESS_SECONDS + 1))
    inactive_runner = LocalRunner(project_id=project.id, token_hash="inactive", status="inactive", last_seen_at=now - timedelta(seconds=_RUNNER_LIVENESS_SECONDS + 1))
    boundary_runner = LocalRunner(project_id=project.id, token_hash="boundary", last_seen_at=now - timedelta(seconds=_RUNNER_LIVENESS_SECONDS))
    task = Task(project_id=project.id, status=TaskStatus.PARKED)
    db_session.add_all([stale_runner, inactive_runner, boundary_runner, task])
    await db_session.commit()

    def job(status=ComputeJobStatus.RUNNING, backend=RUNNER_SUBPROCESS_BACKEND_NAME, **kwargs):
        return ComputeJob(project_id=project.id, backend=backend, status=status, **kwargs)

    stranded_running = job(task_id=task.id, ran_on_runner_id=stale_runner.id)
    stranded_starting = job(status=ComputeJobStatus.STARTING, ran_on_runner_id=inactive_runner.id)
    boundary_job = job(ran_on_runner_id=boundary_runner.id)
    unclaimed_job = job()
    other_backend_job = job(backend="local-subprocess", ran_on_runner_id=stale_runner.id)
    terminal_job = job(status=ComputeJobStatus.FAILED, outcome=ComputeJobOutcome.FAILURE, ran_on_runner_id=stale_runner.id)
    db_session.add_all([stranded_running, stranded_starting, boundary_job, unclaimed_job, other_backend_job, terminal_job])
    await db_session.flush()
    wake = TaskWakeRegistration(task_id=task.id, project_id=project.id, wake_source_kind=compute_jobs.COMPUTE_JOB_TERMINAL_WAKE_KIND, wake_source_ref=str(stranded_running.id), wake_action=WakeAction.FINALIZE)
    db_session.add(wake)
    await db_session.commit()

    session_factory = async_sessionmaker(resolve_async_engine(db_session), expire_on_commit=False, class_=AsyncSession)
    assert (
        await sweep_stranded_runner_subprocess_compute_jobs(
            db_session, now, session_factory=session_factory
        )
        == 2
    )

    await db_session.refresh(stranded_running)
    await db_session.refresh(stranded_starting)
    await db_session.refresh(boundary_job)
    await db_session.refresh(unclaimed_job)
    await db_session.refresh(other_backend_job)
    await db_session.refresh(terminal_job)
    await db_session.refresh(wake)
    assert stranded_running.status == ComputeJobStatus.FAILED
    assert stranded_running.outcome == ComputeJobOutcome.FAILURE
    assert stranded_running.terminal_reason == "runner_subprocess_runner_dead"
    assert stranded_starting.status == ComputeJobStatus.FAILED
    assert stranded_starting.terminal_reason == "runner_subprocess_runner_dead"
    assert wake.fired_at is not None
    history = (await db_session.execute(select(TaskHistory).where(TaskHistory.task_id == task.id))).scalars().all()
    assert any(row.action == "COMPUTE_TERMINATED" for row in history)
    assert boundary_job.status == ComputeJobStatus.RUNNING
    assert unclaimed_job.status == ComputeJobStatus.RUNNING
    assert other_backend_job.status == ComputeJobStatus.RUNNING
    assert terminal_job.status == ComputeJobStatus.FAILED
    assert (
        await sweep_stranded_runner_subprocess_compute_jobs(
            db_session, now, session_factory=session_factory
        )
        == 0
    )


@pytest.mark.asyncio
async def test_stranded_runner_subprocess_sweep_protects_paused_draining_runners(db_session):
    """Paused/draining runners with recent heartbeats keep in-flight jobs; stale ones get finalized."""
    from app.reaper_sweeps import sweep_stranded_runner_subprocess_compute_jobs
    from app.runner_health_monitors import _RUNNER_LIVENESS_SECONDS

    now = _utcnow()
    project = await _make_project_owner(db_session, name="Paused/Draining runner sweep")
    # Paused/draining runners with RECENT heartbeats — still alive, should NOT lose jobs.
    paused_alive = LocalRunner(project_id=project.id, token_hash="paused-alive", status="paused", last_seen_at=now)
    draining_alive = LocalRunner(project_id=project.id, token_hash="draining-alive", status="draining", last_seen_at=now)
    # Paused/draining runners with STALE heartbeats — truly dead, jobs SHOULD be finalized.
    paused_dead = LocalRunner(project_id=project.id, token_hash="paused-dead", status="paused", last_seen_at=now - timedelta(seconds=_RUNNER_LIVENESS_SECONDS + 1))
    draining_dead = LocalRunner(project_id=project.id, token_hash="draining-dead", status="draining", last_seen_at=now - timedelta(seconds=_RUNNER_LIVENESS_SECONDS + 1))
    task = Task(project_id=project.id, status=TaskStatus.PARKED)
    db_session.add_all([paused_alive, draining_alive, paused_dead, draining_dead, task])
    await db_session.commit()

    def job(status=ComputeJobStatus.RUNNING, **kwargs):
        return ComputeJob(project_id=project.id, backend=RUNNER_SUBPROCESS_BACKEND_NAME, status=status, **kwargs)

    paused_alive_job = job(ran_on_runner_id=paused_alive.id)
    draining_alive_job = job(ran_on_runner_id=draining_alive.id)
    paused_dead_job = job(ran_on_runner_id=paused_dead.id)
    draining_dead_job = job(ran_on_runner_id=draining_dead.id)
    db_session.add_all([paused_alive_job, draining_alive_job, paused_dead_job, draining_dead_job])
    await db_session.flush()
    wake = TaskWakeRegistration(
        task_id=task.id, project_id=project.id,
        wake_source_kind=compute_jobs.COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(paused_dead_job.id), wake_action=WakeAction.FINALIZE,
    )
    db_session.add(wake)
    await db_session.commit()

    session_factory = async_sessionmaker(resolve_async_engine(db_session), expire_on_commit=False, class_=AsyncSession)
    # Expected: 2 dead-runner jobs finalized, 2 alive-runner jobs survive.
    assert (
        await sweep_stranded_runner_subprocess_compute_jobs(
            db_session, now, session_factory=session_factory
        )
        == 2
    )

    await db_session.refresh(paused_alive_job)
    await db_session.refresh(draining_alive_job)
    await db_session.refresh(paused_dead_job)
    await db_session.refresh(draining_dead_job)
    # Alive paused/draining: their jobs MUST survive (runner still heartbeating).
    assert paused_alive_job.status == ComputeJobStatus.RUNNING
    assert draining_alive_job.status == ComputeJobStatus.RUNNING
    # Dead paused/draining: their jobs ARE finalized (runner truly gone).
    assert paused_dead_job.status == ComputeJobStatus.FAILED
    assert paused_dead_job.terminal_reason == "runner_subprocess_runner_dead"
    assert draining_dead_job.status == ComputeJobStatus.FAILED
    assert draining_dead_job.terminal_reason == "runner_subprocess_runner_dead"


@pytest.mark.asyncio
async def test_draining_runner_finishes_in_flight_job_without_new_dispatch(
    client, db_session
):
    """Drain is a quiesce contract: preserve active work and refuse new claims.

    The runner-side worker interprets ``cancel_requested: false`` on its normal
    running report as permission to keep the healthy subprocess alive.  This
    exercises that report path through normal completion, while separately
    proving the next-compute-job pull cannot claim work after the runner drains.
    """
    from app.reaper_sweeps import sweep_stranded_runner_subprocess_compute_jobs

    now = _utcnow()
    project = await _make_project_owner(db_session, name="Runner drain contract")
    runner, token = await _register_runner(db_session, project)
    in_flight = await _make_runner_subprocess_job(db_session, project, runner)
    queued = await _make_runner_subprocess_job(
        db_session, project, runner, status=ComputeJobStatus.STARTING
    )

    # This mirrors the admin drain transition after the in-flight subprocess has
    # already been claimed.  The recent heartbeat must continue to prove liveness.
    runner.status = "draining"
    runner.last_seen_at = now
    await db_session.commit()

    running = await client.post(
        f"/api/runner/compute-job/{in_flight.id}/report",
        headers={"Authorization": f"Bearer {token}"},
        json={"exec_status": "running"},
    )
    assert running.status_code == 200, running.text
    assert running.json()["cancel_requested"] is False

    session_factory = async_sessionmaker(
        resolve_async_engine(db_session), expire_on_commit=False, class_=AsyncSession
    )
    assert await sweep_stranded_runner_subprocess_compute_jobs(
        db_session,
        now,
        session_factory=session_factory,
    ) == 0
    await db_session.refresh(in_flight)
    assert in_flight.status == ComputeJobStatus.RUNNING
    assert in_flight.terminal_reason is None

    no_dispatch = await client.post(
        "/api/runner/next-compute-job",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert no_dispatch.status_code == 204
    await db_session.refresh(queued)
    assert queued.status == ComputeJobStatus.STARTING

    completed = await client.post(
        f"/api/runner/compute-job/{in_flight.id}/report",
        headers={"Authorization": f"Bearer {token}"},
        json={"exec_status": "succeeded", "exit_code": 0},
    )
    assert completed.status_code == 200, completed.text
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await db_session.refresh(in_flight)
    assert in_flight.status == ComputeJobStatus.SUCCEEDED
    assert in_flight.terminal_reason != "runner_subprocess_runner_dead"


@pytest.mark.asyncio
async def test_runner_report_exit_125_finalizes_no_progress_timeout_through_monitor(
    client, db_session
):
    """END-TO-END: a runner no-progress watchdog kill (exit 125) reported over the
    HTTP /report endpoint is finalized by the monitor as an intentional TIMED_OUT
    no-progress cut whose durable terminal_reason classifies to NO_PROGRESS_TIMEOUT.

    This is the full pipeline the unit test only samples at poll_status: the
    reserved exit code produced by scripts/local_runner.py's watchdog must survive
    the report endpoint, poll_status' exit-125 translation, and the monitor's
    finalize path, so the platform kill is never misattributed to the operator's
    command (which would burn retry budget)."""
    from app.failure_classification import (
        NO_PROGRESS_TIMEOUT,
        classify_compute_terminal_reason,
    )

    project = await _make_project_owner(db_session, name="Runner NoProgress E2E")
    runner, token = await _register_runner(db_session, project)
    job = await _make_runner_subprocess_job(db_session, project, runner)

    # 1) Runner reports the no-progress watchdog kill over the real HTTP endpoint.
    report = await client.post(
        f"/api/runner/compute-job/{job.id}/report",
        headers={"Authorization": f"Bearer {token}"},
        json={"exec_status": "failed", "exit_code": 125},
    )
    assert report.status_code == 200, report.text

    # 2) The monitor polls the stamped state and finalizes the job.
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.TIMED_OUT
    assert job.outcome == ComputeJobOutcome.TIMED_OUT
    assert (job.polling_metadata or {}).get("exit_code") == 125
    assert job.terminal_reason == "no_progress_timeout"
    assert (job.polling_metadata or {}).get("terminal_kind") == "no_progress"
    # 3) The classifier keys the terminal reason to a platform kill, not a defect.
    assert (
        classify_compute_terminal_reason(job.terminal_reason, None)
        == NO_PROGRESS_TIMEOUT
    )
    result = json.loads(compute_jobs.build_compute_result_logs(job))
    assert result["intentional_terminal"] is True
    assert result["killed_by"] == "deadline"


@pytest.mark.asyncio
async def test_runner_report_exit_124_finalizes_runtime_deadline_through_monitor(
    client, db_session
):
    """END-TO-END: a runner runtime-deadline kill (exit 124) reported over the HTTP
    /report endpoint is finalized as a TIMED_OUT deadline outcome that classifies
    to AGENT_TIMEOUT — the other client-side watchdog, and NOT a CODE_ERROR.

    Guards the sibling of the exit-125 path: exit 124 is recognised as a runtime
    timeout by the monitor (_status_is_runtime_timeout), so the platform deadline
    kill is attributed to the deadline, not to the command."""
    from app.failure_classification import (
        AGENT_TIMEOUT,
        classify_compute_terminal_reason,
    )

    project = await _make_project_owner(db_session, name="Runner Deadline E2E")
    runner, token = await _register_runner(db_session, project)
    job = await _make_runner_subprocess_job(db_session, project, runner)

    report = await client.post(
        f"/api/runner/compute-job/{job.id}/report",
        headers={"Authorization": f"Bearer {token}"},
        json={"exec_status": "failed", "exit_code": 124},
    )
    assert report.status_code == 200, report.text

    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    # Runtime-deadline kills terminalize as TIMED_OUT (matching the server-side
    # runtime_deadline enforcement), not FAILED.
    assert job.status == ComputeJobStatus.TIMED_OUT
    assert job.outcome == ComputeJobOutcome.TIMED_OUT
    assert "runtime deadline" in (job.terminal_reason or "")
    assert (
        classify_compute_terminal_reason(job.terminal_reason, None) == AGENT_TIMEOUT
    )


@pytest.mark.asyncio
async def test_runner_report_generic_failure_finalizes_code_error_through_monitor(
    client, db_session
):
    """CONTROL: a runner report of a plain non-watchdog failure (exit 1) finalizes
    as a FAILURE that classifies to CODE_ERROR.

    Proves the 124/125 special-casing is SPECIFIC — an ordinary command failure is
    still attributed to the command, so the watchdog reasons are not over-applied."""
    from app.failure_classification import (
        CODE_ERROR,
        classify_compute_terminal_reason,
    )

    project = await _make_project_owner(db_session, name="Runner CodeError E2E")
    runner, token = await _register_runner(db_session, project)
    job = await _make_runner_subprocess_job(db_session, project, runner)

    report = await client.post(
        f"/api/runner/compute-job/{job.id}/report",
        headers={"Authorization": f"Bearer {token}"},
        json={"exec_status": "failed", "exit_code": 1},
    )
    assert report.status_code == 200, report.text

    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.FAILED
    assert job.outcome == ComputeJobOutcome.FAILURE
    assert (job.polling_metadata or {}).get("exit_code") == 1
    assert (
        classify_compute_terminal_reason(job.terminal_reason, None) == CODE_ERROR
    )


@pytest.mark.asyncio
async def test_gcp_poll_status_reconciles_terminated_vm_to_failure(monkeypatch):
    """A genuinely-failed job whose serial marker was also lost reconciles to
    FAILED (with its real exit code), not to a vague 'no terminal marker'."""
    backend = GcpComputeBackend()
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend=backend.name,
        external_compute_id="compute-job-vm-failrec",
        status=ComputeJobStatus.RUNNING,
        polling_metadata={"zone": "us-central1-f"},
    )
    monkeypatch.setattr(compute, "get_vm_serial_output", lambda *_a, **_k: "noise")
    monkeypatch.setattr(compute, "get_vm_status", lambda *_a, **_k: "TERMINATED")

    bundle = _make_tar_gz_bundle(
        {
            "./terminal.json": _terminal_record_bytes(
                status="FAILED", exit_code=2, reason="command_failed"
            )
        }
    )

    async def fake_download(_job_id):
        return bundle

    monkeypatch.setattr(storage, "download_compute_job_artifact_bundle", fake_download)

    status = await backend.poll_status(job)
    assert status.status == ComputeJobStatus.FAILED
    assert status.exit_code == 2
    assert status.message == "command_failed"


@pytest.mark.asyncio
async def test_gcp_poll_status_serial_marker_skips_durable_reconcile(monkeypatch):
    """The serial fast-path still wins: when the serial SUCCEEDED marker IS
    present, poll_status finalizes from it and never downloads the bundle."""
    backend = GcpComputeBackend()
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend=backend.name,
        external_compute_id="compute-job-vm-serialwin",
        status=ComputeJobStatus.RUNNING,
        polling_metadata={"zone": "us-central1-f"},
    )
    monkeypatch.setattr(
        compute,
        "get_vm_serial_output",
        lambda *_a, **_k: "\n".join(
            [
                "BLUEPRINT_COMPUTE_REASON=completed",
                "BLUEPRINT_COMPUTE_STATUS=SUCCEEDED",
                "BLUEPRINT_COMPUTE_EXIT_CODE=0",
            ]
        ),
    )

    async def forbid_download(_job_id):
        raise AssertionError(
            "durable reconcile must not run when serial marker present"
        )

    monkeypatch.setattr(
        storage, "download_compute_job_artifact_bundle", forbid_download
    )

    status = await backend.poll_status(job)
    assert status.status == ComputeJobStatus.SUCCEEDED
    assert status.exit_code == 0


@pytest.mark.asyncio
async def test_local_state_file_parse_extracts_result_metrics():
    backend = LocalSubprocessComputeBackend()
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        # Command opts in by writing the designated result file.
        command='echo \'{"accuracy": 0.88, "loss": 0.12}\' > "$BLUEPRINT_RESULT_FILE"',
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
    )
    result = await backend.launch(spec)
    compute_job = ComputeJob(
        id=spec.job_id,
        project_id=uuid.uuid4(),
        backend=backend.name,
        external_compute_id=result.external_id,
        status=ComputeJobStatus.RUNNING,
        polling_metadata={"pid": result.metadata["pid"]},
    )

    for _ in range(60):
        terminal = compute_backends_module._read_local_terminal_status(compute_job)
        if terminal is not None:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("expected local terminal marker for completed subprocess")

    assert terminal.status == ComputeJobStatus.SUCCEEDED
    assert terminal.metrics == {"accuracy": 0.88, "loss": 0.12}
    assert terminal.command_log is None


@pytest.mark.asyncio
async def test_local_state_file_parse_no_result_file_yields_no_metrics():
    backend = LocalSubprocessComputeBackend()
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="exit 0",  # writes no result file
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
    )
    result = await backend.launch(spec)
    compute_job = ComputeJob(
        id=spec.job_id,
        project_id=uuid.uuid4(),
        backend=backend.name,
        external_compute_id=result.external_id,
        status=ComputeJobStatus.RUNNING,
        polling_metadata={"pid": result.metadata["pid"]},
    )

    for _ in range(60):
        terminal = compute_backends_module._read_local_terminal_status(compute_job)
        if terminal is not None:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("expected local terminal marker for completed subprocess")

    assert terminal.status == ComputeJobStatus.SUCCEEDED
    assert terminal.metrics is None
    assert terminal.command_log is None


@pytest.mark.asyncio
async def test_local_compute_job_persists_result_metrics_through_monitor(
    db_session, monkeypatch
):
    """Regression: live-handle exit path must surface metrics just like recovery path.

    When the monitor catches the subprocess still alive in its _processes dict and
    then sees it exit (the live-handle path), the resulting ComputeStatusResult must
    include any metrics written to BLUEPRINT_RESULT_FILE — identical to what the
    restart-recovery path returns from _read_local_terminal_status.
    """
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="Live Exit Metrics", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="live exit metrics parent",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command='echo \'{"accuracy": 0.95, "f1": 0.88}\' > "$BLUEPRINT_RESULT_FILE"',
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name="local-subprocess",
    )

    # Use the real LocalSubprocessComputeBackend — do NOT monkeypatch it away.
    # The singleton _LOCAL_BACKEND is returned by get_compute_backend for the
    # "local-subprocess" name, so the live _processes handle is retained across calls.
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    # Drive the monitor until terminal or timeout (subprocess should finish in <2s).
    for _ in range(60):
        await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
        await db_session.refresh(job)
        if job.status in (ComputeJobStatus.SUCCEEDED, ComputeJobStatus.FAILED):
            break
        await asyncio.sleep(0.1)
    else:
        raise AssertionError(
            f"compute job did not reach terminal status within timeout; status={job.status}"
        )

    assert job.status == ComputeJobStatus.SUCCEEDED
    assert job.polling_metadata.get("result_metrics") == {"accuracy": 0.95, "f1": 0.88}


@pytest.mark.asyncio
async def test_local_backend_artifact_dir_survives_subprocess_exit():
    """The durable artifact-dir contract: a command writes a non-metric file to
    $BLUEPRINT_ARTIFACT_DIR, and that file is still readable from the persisted
    durable location after the subprocess has fully exited."""
    backend = LocalSubprocessComputeBackend()
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command='echo "checkpoint-bytes" > "$BLUEPRINT_ARTIFACT_DIR/model.txt"',
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
    )
    result = await backend.launch(spec)

    # Launch result records the durable artifact location for finalization/tests.
    artifact_dir = result.metadata["artifact_dir"]
    assert result.metadata["artifact_uri"] == f"file://{artifact_dir}"

    compute_job = ComputeJob(
        id=spec.job_id,
        project_id=uuid.uuid4(),
        backend=backend.name,
        external_compute_id=result.external_id,
        status=ComputeJobStatus.RUNNING,
        polling_metadata={"pid": result.metadata["pid"]},
    )

    for _ in range(60):
        if compute_backends_module._read_local_terminal_status(compute_job) is not None:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("expected local terminal marker for completed subprocess")

    # Wait for the subprocess to FULLY exit before asserting it's gone. The
    # terminal marker is written by the wrapper a beat BEFORE the process is
    # reaped, so a bare ``assert not _is_pid_running`` right after the marker
    # poll races the OS reaping the pid and flakes (assert not True). Poll the
    # actual exit condition with the same bounded budget — deterministic, not
    # timing-dependent.
    pid = result.metadata["pid"]
    for _ in range(60):
        if not compute_backends_module._is_pid_running(pid):
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError(
            f"subprocess pid {pid} still running 3s after its terminal marker"
        )
    # Subprocess is gone; the artifact written to the contract dir is still on disk.
    artifact_path = os.path.join(artifact_dir, "model.txt")
    with open(artifact_path, encoding="utf-8") as handle:
        assert handle.read().strip() == "checkpoint-bytes"


async def _await_local_terminal(compute_job) -> None:
    """Poll a local subprocess job's durable terminal marker with a bounded budget."""
    for _ in range(80):
        if compute_backends_module._read_local_terminal_status(compute_job) is not None:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("expected local terminal marker for completed subprocess")


@pytest.mark.asyncio
async def test_input_artifact_job_id_rehydrates_checkpoint_bundle(monkeypatch):
    """End-to-end cross-job checkpoint resume seam.

    A FIRST job writes a checkpoint file to $BLUEPRINT_ARTIFACT_DIR and completes;
    its durable bundle is then served (monkeypatched storage) to a SECOND job
    launched with ``input_artifact_job_id`` pointing at the first. The second job
    starts with that checkpoint file already present in its $BLUEPRINT_ARTIFACT_DIR
    BEFORE its own command runs — proving rehydration happens ahead of execution.
    """
    backend = LocalSubprocessComputeBackend()

    # ── First job: write a checkpoint into the durable artifact dir.
    first_id = uuid.uuid4()
    first_spec = ComputeLaunchSpec(
        job_id=first_id,
        command='echo "step-42" > "$BLUEPRINT_ARTIFACT_DIR/model.ckpt"',
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
    )
    first_result = await backend.launch(first_spec)
    first_artifact_dir = first_result.metadata["artifact_dir"]
    first_job = ComputeJob(
        id=first_id,
        project_id=uuid.uuid4(),
        backend=backend.name,
        external_compute_id=first_result.external_id,
        status=ComputeJobStatus.RUNNING,
        polling_metadata={"pid": first_result.metadata["pid"]},
    )
    await _await_local_terminal(first_job)
    assert os.path.exists(os.path.join(first_artifact_dir, "model.ckpt"))

    # Build the predecessor's durable bundle from its artifact dir, then serve it as
    # the stored bundle for `first_id` (what GCS would hold after upload).
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(first_artifact_dir, arcname=".")
    bundle_bytes = buf.getvalue()

    # Rehydration STREAMS the predecessor bundle to a temp file on disk (never
    # buffers it whole in the orchestrator heap), so the seam it goes through is
    # ``_download_artifact_bundle_to_path`` — mock that, writing the bundle to the
    # destination path exactly as a real streaming download would.
    def fake_stream_to_path(job_id, dest_path):
        if str(job_id) != str(first_id):
            return False
        with open(dest_path, "wb") as handle:
            handle.write(bundle_bytes)
        return True

    monkeypatch.setattr(
        compute_backends_module,
        "_download_artifact_bundle_to_path",
        fake_stream_to_path,
    )

    # ── Second job: launched WITH the predecessor reference; its command copies the
    # rehydrated checkpoint into result.json so the seam is observable via metrics.
    second_id = uuid.uuid4()
    second_spec = ComputeLaunchSpec(
        job_id=second_id,
        command='test -f "$BLUEPRINT_ARTIFACT_DIR/model.ckpt"',
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        input_artifact_job_id=first_id,
    )
    second_result = await backend.launch(second_spec)
    second_artifact_dir = second_result.metadata["artifact_dir"]

    # Rehydration ran synchronously inside launch() BEFORE the subprocess spawned:
    # the predecessor's checkpoint is already present in the new job's artifact dir.
    rehydrated = os.path.join(second_artifact_dir, "model.ckpt")
    assert os.path.exists(rehydrated), "predecessor checkpoint not rehydrated"
    with open(rehydrated, encoding="utf-8") as handle:
        assert handle.read().strip() == "step-42"

    # And the second command (which asserts the file exists) succeeds.
    second_job = ComputeJob(
        id=second_id,
        project_id=uuid.uuid4(),
        backend=backend.name,
        external_compute_id=second_result.external_id,
        status=ComputeJobStatus.RUNNING,
        polling_metadata={"pid": second_result.metadata["pid"]},
    )
    await _await_local_terminal(second_job)
    terminal = compute_backends_module._read_local_terminal_status(second_job)
    assert terminal is not None
    assert terminal.status == ComputeJobStatus.SUCCEEDED
    assert terminal.exit_code == 0


@pytest.mark.asyncio
async def test_input_artifact_rehydration_fails_loudly_when_bundle_missing(monkeypatch):
    """A missing predecessor bundle at run time fails the launch LOUDLY rather than
    letting the command run against an empty workspace."""
    backend = LocalSubprocessComputeBackend()

    # The streaming seam reports a missing object by returning False (no bundle to
    # resume from) — rehydration must convert that into a loud, typed failure.
    def fake_stream_to_path(job_id, dest_path):
        return False

    monkeypatch.setattr(
        compute_backends_module,
        "_download_artifact_bundle_to_path",
        fake_stream_to_path,
    )

    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="echo should-not-run",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        input_artifact_job_id=uuid.uuid4(),
    )
    with pytest.raises(RuntimeError, match="input_artifact_rehydration_failed"):
        await backend.launch(spec)


@pytest.mark.asyncio
async def test_local_backend_resume_records_provenance(monkeypatch):
    """Cross-job checkpoint resume stamps resume_provenance into launch metadata so
    the operator and agent surfaces can distinguish a continued-from-checkpoint run
    from a from-scratch restart."""
    backend = LocalSubprocessComputeBackend()

    # First job: write a checkpoint file.
    first_id = uuid.uuid4()
    first_spec = ComputeLaunchSpec(
        job_id=first_id,
        command='echo "step-42" > "$BLUEPRINT_ARTIFACT_DIR/model.ckpt"',
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
    )
    first_result = await backend.launch(first_spec)
    first_artifact_dir = first_result.metadata["artifact_dir"]
    first_job = ComputeJob(
        id=first_id,
        project_id=uuid.uuid4(),
        backend=backend.name,
        external_compute_id=first_result.external_id,
        status=ComputeJobStatus.RUNNING,
        polling_metadata={"pid": first_result.metadata["pid"]},
    )
    await _await_local_terminal(first_job)

    # Build the predecessor's durable bundle from its artifact dir.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(first_artifact_dir, arcname=".")
    bundle_bytes = buf.getvalue()

    def fake_stream_to_path(job_id, dest_path):
        if str(job_id) != str(first_id):
            return False
        with open(dest_path, "wb") as handle:
            handle.write(bundle_bytes)
        return True

    monkeypatch.setattr(
        compute_backends_module,
        "_download_artifact_bundle_to_path",
        fake_stream_to_path,
    )

    # Second job: launched WITH the predecessor reference.
    second_id = uuid.uuid4()
    second_spec = ComputeLaunchSpec(
        job_id=second_id,
        command="true",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        input_artifact_job_id=first_id,
    )
    second_result = await backend.launch(second_spec)

    # Resume provenance MUST be present in the launch metadata.
    provenance = second_result.metadata.get("resume_provenance")
    assert provenance is not None, "resume_provenance not stamped in launch metadata"
    assert provenance.get("ok") is True, "ok must be True for successful rehydration"
    assert isinstance(provenance.get("file_count"), int), "file_count must be an int"
    assert provenance.get("file_count") >= 1, "at least one file must be rehydrated"
    assert provenance.get("resumed_from_job_id") == str(first_id), \
        "resumed_from_job_id must match the predecessor job id"
    assert isinstance(provenance.get("recorded_at"), str), \
        "recorded_at must be an ISO timestamp"

    # Verify the second command can see the rehydrated file.
    second_job = ComputeJob(
        id=second_id,
        project_id=uuid.uuid4(),
        backend=backend.name,
        external_compute_id=second_result.external_id,
        status=ComputeJobStatus.RUNNING,
        polling_metadata={"pid": second_result.metadata["pid"]},
    )
    await _await_local_terminal(second_job)
    terminal = compute_backends_module._read_local_terminal_status(second_job)
    assert terminal is not None
    assert terminal.status == ComputeJobStatus.SUCCEEDED
    assert terminal.exit_code == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("adoption_path", ["memory", "persisted_pid", "terminal"])
async def test_local_resume_adoption_recovers_durable_provenance(
    monkeypatch, tmp_path, adoption_path
):
    """Every idempotent adoption path recovers proof after DB persistence loss."""
    monkeypatch.setattr(
        compute_backends_module, "_LOCAL_STATE_ROOT", str(tmp_path / "state")
    )
    backend = LocalSubprocessComputeBackend()
    source_id = uuid.uuid4()
    job_id = uuid.uuid4()
    external_id = f"local-{job_id}"
    os.makedirs(
        compute_backends_module._local_state_dir_for_external_id(external_id),
        exist_ok=True,
    )
    provenance = {
        "ok": True,
        "file_count": 4,
        "recorded_at": "2026-07-15T00:00:00+00:00",
        "resumed_from_job_id": str(source_id),
    }
    compute_backends_module._write_local_resume_provenance_for_external_id(
        external_id, provenance
    )

    if adoption_path == "memory":
        proc = MagicMock()
        proc.pid = 4101
        proc.poll.return_value = None
        backend._processes[external_id] = compute_backends_module._LocalProcessHandle(
            proc=proc, started_at=datetime(2026, 7, 15)
        )
    elif adoption_path == "persisted_pid":
        monkeypatch.setattr(
            compute_backends_module,
            "_local_subprocess_pid_for_external_id",
            lambda _external_id: 4102,
        )

        async def running(_pid):
            return True

        monkeypatch.setattr(compute_backends_module, "_aio_is_pid_running", running)
    else:
        compute_backends_module._write_local_state_text(
            compute_backends_module._local_terminal_path_for_external_id(external_id),
            "STATUS=SUCCEEDED\nEXIT_CODE=0",
        )

    result = await backend.launch(
        ComputeLaunchSpec(
            job_id=job_id,
            external_id=external_id,
            command="must-not-run",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_at=None,
            input_artifact_job_id=str(source_id),
        )
    )

    assert result.metadata["resume_provenance"] == provenance


def test_local_resume_provenance_recovery_rejects_mismatched_source(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        compute_backends_module, "_LOCAL_STATE_ROOT", str(tmp_path / "state")
    )
    external_id = "local-source-match"
    os.makedirs(
        compute_backends_module._local_state_dir_for_external_id(external_id),
        exist_ok=True,
    )
    compute_backends_module._write_local_resume_provenance_for_external_id(
        external_id,
        {
            "ok": True,
            "file_count": 1,
            "resumed_from_job_id": str(uuid.uuid4()),
        },
    )
    assert (
        compute_backends_module._read_local_resume_provenance_for_external_id(
            external_id, uuid.uuid4()
        )
        is None
    )


def test_safe_extract_artifact_bundle_rejects_path_traversal(tmp_path):
    """The extractor drops absolute-path and ``..`` traversal members, extracting
    only the safe ones under the destination dir."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def _add(name, data):
            info = tarfile.TarInfo(name=name)
            payload = data.encode()
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

        _add("safe.txt", "ok")
        _add("nested/deep.txt", "ok2")
        _add("../escape.txt", "evil")
        absolute_escape = tmp_path / "absolute-escape.txt"
        _add(str(absolute_escape), "evil")
    bundle = buf.getvalue()

    dest = tmp_path / "artifacts"
    extracted = compute_backends_module._safe_extract_artifact_bundle(
        bundle, str(dest)
    )

    assert extracted == 2
    assert (dest / "safe.txt").exists()
    assert (dest / "nested" / "deep.txt").exists()
    # Neither traversal member escaped the destination dir.
    assert not (tmp_path / "escape.txt").exists()
    assert not absolute_escape.exists()


def test_is_safe_tar_member_guards():
    guard = compute_backends_module._is_safe_tar_member
    assert guard("a/b/c.txt")
    assert guard("./ok.txt")
    assert not guard("")
    assert not guard("/etc/passwd")
    assert not guard("../up.txt")
    assert not guard("a/../../b.txt")
    assert not guard("~/secret")


@pytest.mark.asyncio
async def test_no_progress_watchdog_terminates_stalled_job():
    """The no-progress watchdog cuts a stalled job early with exit code 125
    and terminal reason ``no_progress_timeout`` when the job produces no
    progress signal (does not touch BLUEPRINT_PROGRESS_FILE) within the
    configured window."""
    backend = LocalSubprocessComputeBackend()
    # Use a generous 5s watchdog window — the command sleeps 60s without
    # touching the progress file, so the watchdog fires well before the
    # command would finish naturally.
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="sleep 60",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        no_progress_timeout_seconds=5,
    )
    result = await backend.launch(spec)
    compute_job = ComputeJob(
        id=spec.job_id,
        project_id=uuid.uuid4(),
        backend=backend.name,
        external_compute_id=result.external_id,
        status=ComputeJobStatus.RUNNING,
        polling_metadata={"pid": result.metadata["pid"]},
    )

    # Poll for the terminal marker — the watchdog should fire within
    # ~5s + overhead, well before the 60s sleep expires.
    terminal = None
    for _ in range(80):  # up to ~8s at 0.1s intervals
        terminal = compute_backends_module._read_local_terminal_status(compute_job)
        if terminal is not None:
            break
        await asyncio.sleep(0.1)
    assert terminal is not None, (
        "expected terminal marker from no-progress watchdog, "
        "but none appeared within 8s"
    )
    assert terminal.status == ComputeJobStatus.FAILED
    assert terminal.exit_code == 125
    assert terminal.message == "no_progress_timeout"


@pytest.mark.asyncio
async def test_no_progress_watchdog_inert_when_unset():
    """When no_progress_timeout_seconds is unset, the watchdog is inert and
    the job runs to normal completion without interference."""
    backend = LocalSubprocessComputeBackend()
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="echo ok",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        # no_progress_timeout_seconds is NOT set — watchdog should be inert
    )
    result = await backend.launch(spec)
    compute_job = ComputeJob(
        id=spec.job_id,
        project_id=uuid.uuid4(),
        backend=backend.name,
        external_compute_id=result.external_id,
        status=ComputeJobStatus.RUNNING,
        polling_metadata={"pid": result.metadata["pid"]},
    )

    # Poll for the terminal marker — job should finish naturally.
    terminal = None
    for _ in range(60):
        terminal = compute_backends_module._read_local_terminal_status(compute_job)
        if terminal is not None:
            break
        await asyncio.sleep(0.05)
    assert terminal is not None, "expected terminal marker for completed subprocess"
    assert terminal.status == ComputeJobStatus.SUCCEEDED
    assert terminal.exit_code == 0


@pytest.mark.asyncio
async def test_local_wrapper_preserves_nonzero_exit_code():
    """A failing command's REAL exit code must reach the terminal record.

    Regression test for an ordering bug in the wrapper's else-branch: it did
        STATUS=FAILED
        EXIT_CODE=$?
    so `$?` captured the STATUS *assignment's* status (always 0), not python3's.
    Every failing local/runner job therefore recorded EXIT_CODE=0 — the exit code,
    the one piece of evidence about HOW the command failed, was silently lost.

    Executes the wrapper for real (not a string assertion) so only correct
    ordering can pass.
    """
    backend = LocalSubprocessComputeBackend()
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="exit 42",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
    )
    result = await backend.launch(spec)
    compute_job = ComputeJob(
        id=spec.job_id,
        project_id=uuid.uuid4(),
        backend=backend.name,
        external_compute_id=result.external_id,
        status=ComputeJobStatus.RUNNING,
        polling_metadata={"pid": result.metadata["pid"]},
    )

    terminal = None
    for _ in range(80):
        terminal = compute_backends_module._read_local_terminal_status(compute_job)
        if terminal is not None:
            break
        await asyncio.sleep(0.1)
    assert terminal is not None, "expected terminal marker for failed subprocess"
    assert terminal.status == ComputeJobStatus.FAILED
    # The assertion that fails on the old ordering (it recorded 0).
    assert terminal.exit_code == 42


def test_local_wrapper_captures_exit_code_before_setting_status():
    """Static guard on the emitted order: `$?` is read BEFORE anything else runs.

    Pairs with the e2e test above — this one pins the *shape* so a future edit
    cannot reintroduce the ordering bug even if the e2e test is skipped.
    """
    wrapper = compute_backends_module._build_local_wrapper_command(
        "echo hi", external_id="local-exit-order"
    )
    lines = [line.strip() for line in wrapper.splitlines()]
    assert "EXIT_CODE=$?" in lines
    # In the failure branch, EXIT_CODE=$? must precede STATUS=FAILED.
    assert lines.index("EXIT_CODE=$?") < lines.index("STATUS=FAILED")


def test_local_wrapper_command_provisions_and_exports_artifact_dir():
    artifact_dir = compute_backends_module._local_artifact_dir_for_external_id(
        "local-z"
    )
    wrapper = compute_backends_module._build_local_wrapper_command(
        "echo hi", external_id="local-z"
    )
    assert f"export BLUEPRINT_ARTIFACT_DIR={shlex.quote(artifact_dir)}" in wrapper
    assert f"mkdir -p {shlex.quote(artifact_dir)}" in wrapper
    # The artifact dir is durable: it must NOT be removed by the wrapper.
    # Use trailing newline to avoid matching files inside the dir (e.g. command.log.json).
    assert f"rm -f {shlex.quote(artifact_dir)}\n" not in wrapper
    assert f"rm -rf {shlex.quote(artifact_dir)}" not in wrapper


def test_local_wrapper_ships_command_runner_as_base64_not_heredoc():
    """The local wrapper must deliver the shared command_runner.py as a base64
    blob (the CLAUDE.md pattern for embedding another language) — never as a
    multi-line python heredoc, which has to survive two escape passes and is
    invisible to python linters."""
    wrapper = compute_backends_module._build_local_wrapper_command(
        "echo hi", external_id="local-runner-b64"
    )
    # The same real, lintable file the GCP startup script ships.
    assert compute_backends_module._COMMAND_RUNNER_B64 in wrapper
    assert "base64 -d" in wrapper
    # No embedded-python heredoc.
    assert "<<'PY'" not in wrapper
    # The runner is decoded into the job's private state dir (no /tmp races)
    # and invoked with the bounded-log limit as an argument.
    state_dir = compute_backends_module._local_state_dir_for_external_id(
        "local-runner-b64"
    )
    runner_path = os.path.join(state_dir, "command_runner.py")
    assert shlex.quote(runner_path) in wrapper
    assert f"if python3 {shlex.quote(runner_path)}" in wrapper
    assert str(compute_backends_module._MAX_COMMAND_LOG_BYTES) in wrapper


def test_local_wrapper_wraps_command_in_container_when_image_set(monkeypatch):
    """When container_image is set the wrapper must run the command via docker run.

    Verified: docker run present, state dir mounted to /tmp/blueprint_out,
    BLUEPRINT_RESULT_FILE points inside the container, original command preserved,
    and --gpus all is absent when no GPU is detected.
    """
    monkeypatch.setattr(
        compute_backends_module,
        "_detect_local_gpu_cache",
        None,  # no GPU detected
    )
    external_id = "local-container-test"
    state_dir = compute_backends_module._local_state_dir_for_external_id(external_id)
    wrapper = compute_backends_module._build_local_wrapper_command(
        "python train.py --epochs 10",
        external_id=external_id,
        container_image="nvcr.io/nvidia/tensorflow:25.01-tf2-py3",
    )
    assert "docker run" in wrapper
    assert "--rm" in wrapper
    # State dir must be volume-mounted to the fixed in-container path.
    assert f"{state_dir}:{compute_backends_module._CONTAINER_OUT_DIR}" in wrapper
    # BLUEPRINT_RESULT_FILE must point inside the container (not the host path).
    assert f"BLUEPRINT_RESULT_FILE={compute_backends_module._CONTAINER_OUT_DIR}/result.json" in wrapper
    # The original command must be present (inside the docker invocation).
    assert "python train.py --epochs 10" in wrapper
    # GPU flag must NOT appear when no GPU is detected.
    assert "--gpus" not in wrapper
    # docker pull of the image must be present.
    assert "docker pull" in wrapper
    assert "nvcr.io/nvidia/tensorflow:25.01-tf2-py3" in wrapper
    # Absent-field path (no container_image) must be unchanged.
    plain_wrapper = compute_backends_module._build_local_wrapper_command(
        "echo hi", external_id=external_id
    )
    assert "docker" not in plain_wrapper


def test_local_wrapper_adds_gpus_flag_when_gpu_detected(monkeypatch):
    """--gpus all is injected when a local GPU is detected and container_image is set."""
    monkeypatch.setattr(
        compute_backends_module,
        "_detect_local_gpu_cache",
        {"name": "Tesla T4", "memory_mib": 16384},
    )
    wrapper = compute_backends_module._build_local_wrapper_command(
        "echo hi",
        external_id="local-gpu-test",
        container_image="nvcr.io/nvidia/cuda:12.0-base",
    )
    assert "--gpus all" in wrapper


def test_local_wrapper_docker_login_included_when_registry_auth_has_password(monkeypatch):
    """docker login command is included in the wrapper when registry auth has a password."""
    monkeypatch.setattr(
        compute_backends_module,
        "_detect_local_gpu_cache",
        None,
    )
    wrapper = compute_backends_module._build_local_wrapper_command(
        "python train.py",
        external_id="local-auth-test",
        container_image="nvcr.io/nvidia/tensorflow:25.01-tf2-py3",
        container_registry_auth={
            "server": "nvcr.io",
            "username": "$oauthtoken",
            "password": "secret-ngc-key",
        },
    )
    assert "docker login" in wrapper
    assert "nvcr.io" in wrapper
    assert "--password-stdin" in wrapper
    # Password must NOT appear in plain text in the wrapper (passed via printf).
    assert "secret-ngc-key" in wrapper  # present inside printf expression
    assert "docker run" in wrapper


def test_local_wrapper_exports_progress_file_env():
    """The local wrapper always exports BLUEPRINT_PROGRESS_FILE pointing at the
    job's progress file in the state dir."""
    external_id = "local-progress-test"
    progress_file = compute_backends_module._local_progress_file_path_for_external_id(
        external_id
    )
    wrapper = compute_backends_module._build_local_wrapper_command(
        "echo ok", external_id=external_id
    )
    assert f"export BLUEPRINT_PROGRESS_FILE={shlex.quote(progress_file)}" in wrapper


def test_local_wrapper_includes_watchdog_when_no_progress_timeout_set():
    """When no_progress_timeout_seconds is set, the wrapper includes the no-progress
    watchdog subprocess that monitors the progress file."""
    external_id = "local-np-test"
    wrapper = compute_backends_module._build_local_wrapper_command(
        "echo ok",
        external_id=external_id,
        no_progress_timeout_seconds=120,
    )
    assert "_NP_WATCHDOG_PID" in wrapper
    assert "no_progress_timeout" in wrapper
    assert "EXIT_CODE=125" in wrapper
    # Cleanup must kill the watchdog.
    assert 'kill "$_NP_WATCHDOG_PID"' in wrapper
    # The watchdog must be a SLIDING window (loop that resets the deadline when
    # the progress mtime advances), not a single-shot sleep-then-compare — a job
    # that progresses once then stalls must still be cut on the next quiet
    # interval, matching the GCP startup-script watchdog.
    assert "while true; do" in wrapper
    assert "_NP_START=$_NP_NOW" in wrapper


def test_local_wrapper_no_watchdog_when_no_progress_timeout_unset():
    """When no_progress_timeout_seconds is not set, the wrapper does NOT include
    the watchdog subprocess."""
    external_id = "local-np-off-test"
    wrapper = compute_backends_module._build_local_wrapper_command(
        "echo ok",
        external_id=external_id,
        no_progress_timeout_seconds=None,
    )
    assert "_NP_WATCHDOG_PID" not in wrapper


def test_local_container_mounts_repo_and_passes_env(monkeypatch):
    """When repo_clone + env are set, the local wrapper mounts the repo root into the
    container as /workspace, sets PYTHONPATH=/workspace, sets the workdir to /workspace,
    and passes the declared env as -e flags. Also verifies container_image_pull_failed
    is emitted as a typed reason when docker pull fails.
    """
    monkeypatch.setattr(
        compute_backends_module,
        "_detect_local_gpu_cache",
        None,  # no GPU
    )
    from pathlib import Path

    external_id = "local-repo-env-parity-test"
    wrapper = compute_backends_module._build_local_wrapper_command(
        "python train.py",
        external_id=external_id,
        container_image="nvcr.io/nvidia/cuda:12.0-base",
        repo_clone={"owner": "acme", "repo": "ml", "ref": "main"},
        env={"MY_SEED": "42", "DATASET": "/data/train"},
    )
    # The local repo root (three levels above compute_backends.py) must be
    # bind-mounted at /workspace inside the container.
    expected_repo_root = str(Path(compute_backends_module.__file__).parent.parent.parent)
    assert f"{expected_repo_root}:/workspace" in wrapper
    # Working directory inside the container must be /workspace.
    assert "-w" in wrapper
    assert "/workspace" in wrapper
    # PYTHONPATH must be set so in-container imports resolve.
    assert "PYTHONPATH=/workspace" in wrapper
    # Each declared env var must appear as a -e flag in the docker run invocation.
    assert "MY_SEED=42" in wrapper
    assert "DATASET=/data/train" in wrapper
    # Pull failure must emit the typed terminal reason, not fall through silently.
    assert "container_image_pull_failed" in wrapper
    assert "REASON=container_image_pull_failed" in wrapper


def test_gcp_startup_script_uploads_artifacts_to_per_job_prefix():
    job_id = uuid.uuid4()
    spec = ComputeLaunchSpec(
        job_id=job_id,
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=300,
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )

    assert "export BLUEPRINT_ARTIFACT_DIR=" in script
    assert (
        "mkdir -p" in script
        and compute_backends_module._GCP_ARTIFACT_DIR_PATH in script
    )
    assert "command.log.json" in script
    # Upload targets ONLY this job's bundle object, bounded by a hard timeout, best-effort.
    assert "trap upload_artifacts EXIT HUP INT TERM QUIT" in script
    assert "https://storage.example.com/upload" in script
    assert "curl --fail --silent --show-error" in script
    assert "/tmp/blueprint_command_runner.py" in script
    assert "BLUEPRINT_COMPUTE_COMMAND_LOG=" in script
    assert "TTL_SECONDS=330" in script
    assert "runtime_ttl" in script
    assert (
        f"timeout {compute_backends_module._GCP_ARTIFACT_UPLOAD_TIMEOUT_SECONDS}"
        in script
    )
    # Upload must never change the job's exit code (always `|| true`).
    assert "|| true" in script


async def test_process_compute_jobs_once_persists_result_metrics(
    db_session, monkeypatch
):
    project = Project(name="Compute Job Metrics", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute parent",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo ok",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    # Skip the pre-flight smoke gate — this test exercises post-smoke lifecycle.
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    job.polling_metadata = meta
    await db_session.commit()

    class FakeBackend:
        def external_id_for(self, persisted_job):
            return "fake-job-metrics"

        async def launch(self, spec):
            return ComputeLaunchResult(
                external_id="fake-job-metrics",
                started_at=_utcnow() - timedelta(seconds=10),
                cost_rate_per_second=0.0,
                message="launched",
            )

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.01, measured_at=as_of)

        async def poll_status(self, persisted_job):
            return ComputeStatusResult(
                status=ComputeJobStatus.SUCCEEDED,
                exit_code=0,
                finished_at=_utcnow(),
                message="completed",
                metrics={"accuracy": 0.97, "tokens_per_sec": 512.0},
            )

        async def terminate(self, persisted_job, *, reason):
            raise AssertionError("terminate should not be called")

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeBackend()
    )
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.SUCCEEDED
    assert job.polling_metadata["result_metrics"] == {
        "accuracy": 0.97,
        "tokens_per_sec": 512.0,
    }


@pytest.mark.asyncio
async def test_process_compute_jobs_once_launches_and_records_terminal_success(
    db_session, monkeypatch
):
    project = Project(name="Compute Job Success", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute parent",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "research"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo ok",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    assert job.status == ComputeJobStatus.QUEUED
    # Skip the pre-flight smoke gate — this test exercises post-smoke lifecycle.
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    job.polling_metadata = meta
    await db_session.commit()

    started_at = _utcnow() - timedelta(seconds=30)

    class FakeBackend:
        def external_id_for(self, persisted_job):
            return "fake-job-1"

        async def launch(self, spec):
            # The monitor reserves the external id (durable STARTING commit)
            # before launch — the spec carries it so launch is idempotent.
            assert spec.external_id == "fake-job-1"
            return ComputeLaunchResult(
                external_id="fake-job-1",
                started_at=started_at,
                cost_rate_per_second=0.001,
                message="launched",
            )

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(
                total_cost_usd=0.03,
                measured_at=as_of,
            )

        async def poll_status(self, persisted_job):
            return ComputeStatusResult(
                status=ComputeJobStatus.SUCCEEDED,
                exit_code=0,
                finished_at=_utcnow(),
                message="completed",
            )

        async def terminate(self, persisted_job, *, reason):
            raise AssertionError("terminate should not be called")

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeBackend()
    )
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.SUCCEEDED
    assert job.outcome == ComputeJobOutcome.SUCCESS
    assert job.external_compute_id == "fake-job-1"
    assert job.accrued_cost_usd == pytest.approx(0.03)
    assert job.final_cost_usd == pytest.approx(0.03)
    assert job.polling_metadata["exit_code"] == 0
    assert job.polling_metadata["terminal_status"] == ComputeJobStatus.SUCCEEDED.value


@pytest.mark.parametrize(
    "log_text, expected",
    [
        (
            "ERROR: Detected Tesla V100-SXM2-16GB GPU, which is not supported by "
            "this container\nERROR: No supported GPU(s) detected to run this "
            "container",
            True,
        ),
        # GPU-model line alone still trips it (NGC entrypoint phrasing).
        ("Detected A40 GPU, which is not supported by this container", True),
        # A normal successful run is not a false positive.
        ("throughput_tflops: 42.0\nrun complete", False),
        # CUDA error 209: image lacks device code for host GPU arch (e.g. sm_70/Volta).
        (
            "CUDA error: no kernel image is available for execution on the device",
            True,
        ),
        # Non-fatal mentions of "gpu" and "supported" must not trip it.
        ("Using GPU 0; mixed precision supported", False),
    ],
)
def test_status_reports_no_usable_gpu(log_text, expected):
    status = ComputeStatusResult(
        status=ComputeJobStatus.SUCCEEDED,
        exit_code=0,
        command_log={"text": log_text},
    )
    assert compute_jobs._status_reports_no_usable_gpu(status) is expected


def test_status_reports_no_usable_gpu_handles_missing_log():
    # No command log at all — must not raise, must be False.
    assert (
        compute_jobs._status_reports_no_usable_gpu(
            ComputeStatusResult(status=ComputeJobStatus.SUCCEEDED, exit_code=0)
        )
        is False
    )
    # Malformed command_log (text not a string) is tolerated.
    assert (
        compute_jobs._status_reports_no_usable_gpu(
            ComputeStatusResult(
                status=ComputeJobStatus.SUCCEEDED,
                exit_code=0,
                command_log={"text": None},
            )
        )
        is False
    )


@pytest.mark.asyncio
async def test_process_compute_jobs_once_reclassifies_no_usable_gpu_as_failure(
    db_session, monkeypatch
):
    """A container that exits 0 but reports no usable GPU is a FAILURE.

    Regression guard for a real incident: an fp16/fp32 matmul GPU experiment ran
    on an operator V100 under ``nvcr.io/nvidia/pytorch:25.01-py3`` (Volta not
    supported by that image). The entrypoint printed a fatal "No supported
    GPU(s) detected" error and exited 0, so the backend reported SUCCEEDED and
    the job was recorded outcome=SUCCESS despite never producing its metric.
    """
    project = Project(name="No Usable GPU", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="gpu matmul experiment",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "research"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python bench.py",
        machine_type="e2-standard-2",
        spend_cap_usd=5.0,
        deadline_seconds=300,
    )
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    job.polling_metadata = meta
    await db_session.commit()

    gpu_error_log = (
        "Unable to find image 'nvcr.io/nvidia/pytorch:25.01-py3' locally\n"
        "ERROR: Detected Tesla V100-SXM2-16GB GPU, which is not supported by "
        "this container\n"
        "ERROR: No supported GPU(s) detected to run this container"
    )

    class FakeBackend:
        def external_id_for(self, persisted_job):
            return "no-gpu-job"

        async def launch(self, spec):
            return ComputeLaunchResult(
                external_id="no-gpu-job",
                started_at=_utcnow() - timedelta(seconds=30),
                cost_rate_per_second=0.0,
                message="launched",
            )

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.0, measured_at=as_of)

        async def poll_status(self, persisted_job):
            # Container exited 0 despite the fatal GPU-incompatibility error.
            return ComputeStatusResult(
                status=ComputeJobStatus.SUCCEEDED,
                exit_code=0,
                finished_at=_utcnow(),
                message="completed",
                command_log={"text": gpu_error_log},
            )

        async def terminate(self, persisted_job, *, reason):
            raise AssertionError("terminate should not be called")

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeBackend()
    )
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    # Outcome is FAILURE, not SUCCESS, even though the container exited 0.
    assert job.outcome == ComputeJobOutcome.FAILURE
    assert "no usable GPU" in (job.terminal_reason or "")


def _job_with_declared_metric(metric: str | None) -> ComputeJob:
    meta: dict = {}
    if metric is not None:
        meta["declared_metric"] = metric
    return ComputeJob(
        command="python bench.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        status=ComputeJobStatus.RUNNING,
        polling_metadata=meta,
    )


def test_declared_metric_unmeasured_present_in_result_file():
    # Declared metric captured directly in $BLUEPRINT_RESULT_FILE metrics → measured.
    job = _job_with_declared_metric("accuracy")
    status = ComputeStatusResult(
        status=ComputeJobStatus.SUCCEEDED,
        exit_code=0,
        metrics={"accuracy": 0.91, "loss": 0.2},
    )
    assert compute_jobs._declared_metric_unmeasured(job, status) is False


def test_declared_metric_unmeasured_present_in_one_arm():
    # Multi-arm result: the declared metric lives inside an arm block (best-arm
    # semantics the success-criterion evaluator uses) → measured.
    job = _job_with_declared_metric("median_matmul_ms")
    status = ComputeStatusResult(
        status=ComputeJobStatus.SUCCEEDED,
        exit_code=0,
        metrics={
            "arms": {
                "fp32": {"median_matmul_ms": 12.4, "elapsed_seconds": 5.0},
                "fp16": {"median_matmul_ms": 6.1, "elapsed_seconds": 5.0},
            }
        },
    )
    assert compute_jobs._declared_metric_unmeasured(job, status) is False


def test_declared_metric_unmeasured_present_via_stdout_fallback():
    # No result-file metrics, but the command printed its result object to stdout —
    # the stdout fallback recovers it exactly as build_compute_result_logs does.
    job = _job_with_declared_metric("throughput_tflops")
    status = ComputeStatusResult(
        status=ComputeJobStatus.SUCCEEDED,
        exit_code=0,
        command_log={"text": "run complete\n{\"throughput_tflops\": 42.0}\n"},
    )
    assert compute_jobs._declared_metric_unmeasured(job, status) is False


def test_declared_metric_unmeasured_case_insensitive_match():
    # Matches the evaluator's case-insensitive metric lookup.
    job = _job_with_declared_metric("Accuracy")
    status = ComputeStatusResult(
        status=ComputeJobStatus.SUCCEEDED,
        exit_code=0,
        metrics={"accuracy": 0.5},
    )
    assert compute_jobs._declared_metric_unmeasured(job, status) is False


def test_declared_metric_unmeasured_absent_everywhere():
    # Declared metric captured NOWHERE — result file empty, no arm, no stdout → the
    # unmeasured case that must be reclassified.
    job = _job_with_declared_metric("accuracy")
    status = ComputeStatusResult(
        status=ComputeJobStatus.SUCCEEDED,
        exit_code=0,
        metrics={"loss": 0.2},
        command_log={"text": "training finished, no accuracy printed"},
    )
    assert compute_jobs._declared_metric_unmeasured(job, status) is True


def test_declared_metric_unmeasured_no_declared_metric_is_false():
    # A job that declares NO metric is never unmeasured (byte-for-byte unaffected).
    job = _job_with_declared_metric(None)
    status = ComputeStatusResult(
        status=ComputeJobStatus.SUCCEEDED, exit_code=0, metrics=None
    )
    assert compute_jobs._declared_metric_unmeasured(job, status) is False


def test_declared_metric_unmeasured_tolerates_malformed_status():
    # Missing/malformed metrics and command_log must be tolerated, not raise.
    job = _job_with_declared_metric("accuracy")
    status = ComputeStatusResult(
        status=ComputeJobStatus.SUCCEEDED,
        exit_code=0,
        metrics=None,
        command_log={"text": None},
    )
    assert compute_jobs._declared_metric_unmeasured(job, status) is True
    # Non-numeric declared value does not count as measured (matches evaluator).
    job2 = _job_with_declared_metric("accuracy")
    status2 = ComputeStatusResult(
        status=ComputeJobStatus.SUCCEEDED,
        exit_code=0,
        metrics={"accuracy": "n/a"},
    )
    assert compute_jobs._declared_metric_unmeasured(job2, status2) is True


@pytest.mark.asyncio
async def test_declared_metric_missing_reclassified_not_success(
    db_session, monkeypatch
):
    """A declared-metric job that exits 0 writing NO metric is FAILURE, not SUCCESS.

    Regression guard for the recurring silent-SUCCESS waste class (operator bug
    reports b8879c55 / daa28062 / 87baf5fe): a multi-hour runner-subprocess run
    declared a metric, exited 0, but never wrote that metric anywhere — result
    file, per-arm block, or stdout. It was recorded outcome=SUCCESS despite
    measuring nothing. It must reclassify to FAILURE via the no-data guard, whose
    precise resolver confirms the declared scalar metric is unmeasured.
    """
    project = Project(name="Metric Missing Success", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="declared-metric experiment",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=5.0,
        deadline_seconds=300,
        declared_metric="val_accuracy",
    )
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    job.polling_metadata = meta
    await db_session.commit()

    class FakeBackend:
        def external_id_for(self, persisted_job):
            return "metricless-job"

        async def launch(self, spec):
            return ComputeLaunchResult(
                external_id="metricless-job",
                started_at=_utcnow() - timedelta(seconds=30),
                cost_rate_per_second=0.0,
                message="launched",
            )

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.0, measured_at=as_of)

        async def poll_status(self, persisted_job):
            # Exited 0, produced NO metric anywhere — the silent waste case.
            return ComputeStatusResult(
                status=ComputeJobStatus.SUCCEEDED,
                exit_code=0,
                finished_at=_utcnow(),
                message="completed",
                metrics=None,
                command_log={"text": "epoch 1..10 done\n"},
            )

        async def terminate(self, persisted_job, *, reason):
            raise AssertionError("terminate should not be called")

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeBackend()
    )
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    # Reclassified: FAILURE, not a clean SUCCESS.
    assert job.outcome == ComputeJobOutcome.FAILURE
    # Unified no-data terminal reason (the declared scalar metric was unmeasured).
    assert "no usable result" in (job.terminal_reason or "")


@pytest.mark.asyncio
async def test_declared_metric_written_still_success(db_session, monkeypatch):
    """A declared-metric job that WRITES the metric stays SUCCESS (no false positive)."""
    project = Project(name="Metric Written Success", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="declared-metric experiment measured",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=5.0,
        deadline_seconds=300,
        declared_metric="val_accuracy",
    )
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    job.polling_metadata = meta
    await db_session.commit()

    class FakeBackend:
        def external_id_for(self, persisted_job):
            return "measured-job"

        async def launch(self, spec):
            return ComputeLaunchResult(
                external_id="measured-job",
                started_at=_utcnow() - timedelta(seconds=30),
                cost_rate_per_second=0.0,
                message="launched",
            )

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.0, measured_at=as_of)

        async def poll_status(self, persisted_job):
            return ComputeStatusResult(
                status=ComputeJobStatus.SUCCEEDED,
                exit_code=0,
                finished_at=_utcnow(),
                message="completed",
                metrics={"val_accuracy": 0.87},
            )

        async def terminate(self, persisted_job, *, reason):
            raise AssertionError("terminate should not be called")

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeBackend()
    )
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    assert job.outcome == ComputeJobOutcome.SUCCESS


@pytest.mark.parametrize(
    "metrics,expected",
    [
        (None, True),
        ({}, True),
        ("not-a-dict", True),
        ({"accuracy": 0.9}, False),
    ],
)
def test_status_has_no_reported_metrics(metrics, expected):
    status = ComputeStatusResult(
        status=ComputeJobStatus.SUCCEEDED,
        exit_code=0,
        metrics=metrics,
    )
    assert compute_jobs._status_has_no_reported_metrics(status) is expected


def test_job_expects_data_variants():
    # Declared metric stamped in polling_metadata (job-creation path) → expects data.
    job = ComputeJob(polling_metadata={"declared_metric": "val/loss"})
    assert compute_jobs._job_expects_data(job, None) is True

    # Arms block on the task payload → expects data even with no declared metric.
    empty_job = ComputeJob(polling_metadata={})
    arms_task = Task(payloads=[{"arms": {"a": {"metric": 1.0}}}])
    assert compute_jobs._job_expects_data(empty_job, arms_task) is True

    # success_criterion.metric on the task payload → expects data.
    crit_task = Task(payloads=[{"success_criterion": {"metric": "acc"}}])
    assert compute_jobs._job_expects_data(empty_job, crit_task) is True

    # Pure side-effect job: no declared metric, no arms, no criterion → no data expected.
    side_effect_task = Task(payloads=[{"type": "research"}])
    assert compute_jobs._job_expects_data(empty_job, side_effect_task) is False
    assert compute_jobs._job_expects_data(empty_job, None) is False

    # criterion-free compute_experiment: _job_expects_data returns False (it handles
    # metric/arms only); the separate _is_criterion_free_experiment path catches this.
    experiment_no_criterion = Task(payloads=[{"type": "compute_experiment"}])
    assert compute_jobs._job_expects_data(empty_job, experiment_no_criterion) is False
    assert compute_jobs._is_criterion_free_experiment(empty_job, experiment_no_criterion) is True

    # compute_experiment WITH a stamped declared_metric in polling_metadata → not criterion-free.
    job_with_metric = ComputeJob(polling_metadata={"declared_metric": "acc"})
    assert compute_jobs._is_criterion_free_experiment(job_with_metric, experiment_no_criterion) is False

    # compute_experiment WITH a success_criterion in payload → not criterion-free.
    experiment_with_criterion = Task(payloads=[{"type": "compute_experiment", "success_criterion": {"metric": "acc"}}])
    assert compute_jobs._is_criterion_free_experiment(empty_job, experiment_with_criterion) is False

    # Non-experiment type (research) → never criterion-free.
    assert compute_jobs._is_criterion_free_experiment(empty_job, side_effect_task) is False
    assert compute_jobs._is_criterion_free_experiment(empty_job, None) is False


async def _run_no_data_guard_job(
    db_session,
    monkeypatch,
    *,
    task_payload,
    status_metrics=None,
    artifact_reference=None,
):
    """Drive a SUCCEEDED metric-less compute job through the monitor.

    Shared harness for the no-data-guard tests: creates a job whose backend
    reports SUCCEEDED/exit 0 with the given ``status_metrics``, stubs
    durable-artifact detection to return ``artifact_reference``, runs two
    monitor ticks, and returns the refreshed job so the caller can assert the
    resulting outcome/terminal_reason.
    """
    project = Project(name="No Data Guard", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute experiment",
        status=TaskStatus.IN_PROGRESS,
        payloads=[task_payload],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python run.py",
        machine_type="e2-standard-2",
        spend_cap_usd=5.0,
        deadline_seconds=300,
    )
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    declared_metric = task_payload.get("declared_metric")
    criterion = task_payload.get("success_criterion")
    if not declared_metric and isinstance(criterion, dict):
        declared_metric = criterion.get("metric")
    if isinstance(declared_metric, str) and declared_metric.strip():
        # Production job creation stamps this launch-contract value. Mirror that
        # durable declaration so the no-data harness exercises the precise
        # terminal resolver rather than only task-payload fallback behavior.
        meta["declared_metric"] = declared_metric.strip()
    job.polling_metadata = meta
    await db_session.commit()

    class FakeBackend:
        def external_id_for(self, persisted_job):
            return "no-data-job"

        async def launch(self, spec):
            return ComputeLaunchResult(
                external_id="no-data-job",
                started_at=_utcnow() - timedelta(seconds=30),
                cost_rate_per_second=0.0,
                message="launched",
            )

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.0, measured_at=as_of)

        async def poll_status(self, persisted_job):
            return ComputeStatusResult(
                status=ComputeJobStatus.SUCCEEDED,
                exit_code=0,
                finished_at=_utcnow(),
                message="completed",
                metrics=status_metrics,
            )

        async def terminate(self, persisted_job, *, reason):
            raise AssertionError("terminate should not be called")

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeBackend()
    )

    async def _fake_collect(_job):
        return artifact_reference

    monkeypatch.setattr(
        compute_jobs, "collect_terminal_artifact_reference", _fake_collect
    )

    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await db_session.refresh(job)
    return job


@pytest.mark.asyncio
async def test_process_compute_jobs_once_reclassifies_no_data_success_as_failure(
    db_session, monkeypatch
):
    """A metric-declaring job that exits 0 with no metrics AND no artifacts fails.

    Regression guard for the documented silent metric-less SUCCESS incidents: the
    command exited cleanly but produced neither a reported metric nor any durable
    artifact, so it never generated a usable result — it must be reclassified as a
    FAILURE, not recorded as a clean SUCCESS.
    """
    job = await _run_no_data_guard_job(
        db_session,
        monkeypatch,
        task_payload={"type": "research", "declared_metric": "val/loss"},
        status_metrics=None,
        artifact_reference=None,
    )
    assert job.outcome == ComputeJobOutcome.FAILURE
    assert "no usable result" in (job.terminal_reason or "")


@pytest.mark.asyncio
async def test_process_compute_jobs_once_reclassifies_arms_no_data_as_failure(
    db_session, monkeypatch
):
    """The arms sweep path is especially vulnerable — its preflight metric check is
    skipped — so a metric-less, artifact-less arms job must still fail here."""
    job = await _run_no_data_guard_job(
        db_session,
        monkeypatch,
        task_payload={"type": "research", "arms": {"fp16": {"metric": "tflops"}}},
        status_metrics={"elapsed_seconds": 12.0},
        artifact_reference=None,
    )
    assert job.outcome == ComputeJobOutcome.FAILURE
    assert "no usable result" in (job.terminal_reason or "")


@pytest.mark.asyncio
async def test_no_data_guard_respects_reported_metrics(db_session, monkeypatch):
    """A metric-declaring job that DID report metrics stays SUCCESS."""
    job = await _run_no_data_guard_job(
        db_session,
        monkeypatch,
        task_payload={"type": "research", "declared_metric": "accuracy"},
        status_metrics={"accuracy": 0.91},
        artifact_reference=None,
    )
    assert job.outcome == ComputeJobOutcome.SUCCESS


@pytest.mark.asyncio
async def test_compute_experiment_without_criterion_fails(db_session, monkeypatch):
    """A compute_experiment that declares no metric and no success_criterion must FAIL.

    compute_experiment is a measurement task by definition; a metric is mandatory.
    The _job_expects_data carve-out was designed for pure side-effect commands (no
    declared metric), not for experiments — this regression guard ensures a criterion-
    free experiment is reclassified as FAILURE rather than recorded as clean SUCCESS.
    """
    project = Project(name="No Metric Declared Experiment", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute_experiment with no metric",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python bench.py",
        machine_type="e2-standard-2",
        spend_cap_usd=5.0,
        deadline_seconds=300,
    )
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    job.polling_metadata = meta
    await db_session.commit()
    # No declared metric was persisted.
    assert "declared_metric" not in (job.polling_metadata or {})

    class FakeBackend:
        def external_id_for(self, persisted_job):
            return "no-criterion-experiment-job"

        async def launch(self, spec):
            return ComputeLaunchResult(
                external_id="no-criterion-experiment-job",
                started_at=_utcnow() - timedelta(seconds=30),
                cost_rate_per_second=0.0,
                message="launched",
            )

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.0, measured_at=as_of)

        async def poll_status(self, persisted_job):
            return ComputeStatusResult(
                status=ComputeJobStatus.SUCCEEDED,
                exit_code=0,
                finished_at=_utcnow(),
                message="completed",
                metrics=None,
            )

        async def terminate(self, persisted_job, *, reason):
            raise AssertionError("terminate should not be called")

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeBackend()
    )
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    assert job.outcome == ComputeJobOutcome.FAILURE
    assert "compute_experiment declared no metric" in (job.terminal_reason or "")


@pytest.mark.asyncio
async def test_side_effect_job_no_metric_stays_success(db_session, monkeypatch):
    """Side-effect jobs (non-experiment type) with no metric stay SUCCESS.

    The _job_expects_data carve-out must remain intact for non-experiment task types
    (research, plan, etc.) that legitimately have no declared metric.  Only
    compute_experiment tasks are required to declare a criterion.
    """
    job = await _run_no_data_guard_job(
        db_session,
        monkeypatch,
        task_payload={"type": "research"},
        status_metrics=None,
        artifact_reference=None,
    )
    assert job.outcome == ComputeJobOutcome.SUCCESS


@pytest.mark.asyncio
async def test_no_data_guard_respects_durable_artifacts(db_session, monkeypatch):
    """Incidental artifacts do not fulfill an unmeasured declared metric."""
    reference = TerminalArtifactReference(
        uri="file:///tmp/artifacts",
        backend="local",
        file_count=3,
        total_size_bytes=1024,
    )
    job = await _run_no_data_guard_job(
        db_session,
        monkeypatch,
        task_payload={"type": "research", "declared_metric": "val/loss"},
        status_metrics={"elapsed_seconds": 12.0},
        artifact_reference=reference,
    )
    assert job.outcome == ComputeJobOutcome.FAILURE
    assert "never measured its declared result" in (job.terminal_reason or "")
    assert "durable artifacts preserved" in (job.terminal_reason or "")
    assert "uri=file:///tmp/artifacts" in (job.terminal_reason or "")
    assert "file_count=3" in (job.terminal_reason or "")


@pytest.mark.asyncio
async def test_no_data_guard_reclassifies_arms_with_incidental_artifact(
    db_session, monkeypatch
):
    """An arms job needs per-arm data; a stray artifact must not rescue SUCCESS."""
    reference = TerminalArtifactReference(
        uri="file:///tmp/arms-checkpoint",
        backend="local",
        file_count=1,
        total_size_bytes=512,
    )
    job = await _run_no_data_guard_job(
        db_session,
        monkeypatch,
        task_payload={"type": "research", "arms": {"fp16": {"metric": "tflops"}}},
        status_metrics={"elapsed_seconds": 12.0},
        artifact_reference=reference,
    )
    assert job.outcome == ComputeJobOutcome.FAILURE
    assert "never measured its declared result" in (job.terminal_reason or "")
    assert "uri=file:///tmp/arms-checkpoint" in (job.terminal_reason or "")


@pytest.mark.asyncio
async def test_failed_arms_terminal_reason_names_each_arm_failure(
    db_session, monkeypatch
):
    """The aggregator already diagnoses each arm; the terminal reason must say it.

    A three-arm sweep whose arms all died reported only "no usable result". The
    per-arm reason and stderr tail existed on the record the whole time, so the
    operator -- and the agent authoring the next revision -- had to guess. Three
    consecutive live runs of the same experiment were burned that way.
    """
    job = await _run_no_data_guard_job(
        db_session,
        monkeypatch,
        task_payload={
            "type": "research",
            "declared_metric": "env_steps_per_sec",
            "arms": {"cpu": {}, "gpu": {}},
        },
        status_metrics={
            "arms": {
                "cpu": {
                    "error": "arm crashed (exit 1): result file not written",
                    "exit_code": 1,
                    "elapsed_seconds": 12.0,
                    "stderr_excerpt": "ModuleNotFoundError: No module named 'mujoco'",
                },
                "gpu": {"error": "arm timed out (exit 124)", "exit_code": 124},
            }
        },
        artifact_reference=None,
    )
    reason = job.terminal_reason or ""
    assert job.outcome == ComputeJobOutcome.FAILURE
    assert "never measured its declared result" in reason
    assert "cpu: arm crashed (exit 1): result file not written" in reason
    assert "No module named 'mujoco'" in reason
    assert "gpu: arm timed out (exit 124)" in reason


@pytest.mark.asyncio
async def test_arms_bookkeeping_alone_is_not_reported_data(db_session, monkeypatch):
    """exit_code and elapsed_seconds are numeric, but they measure nothing.

    Every arm entry carries them, so counting them as data would let a sweep in
    which EVERY arm crashed record a clean SUCCESS -- the exact silent success
    the no-data guard exists to catch.
    """
    job = await _run_no_data_guard_job(
        db_session,
        monkeypatch,
        task_payload={"type": "research", "arms": {"cpu": {"metric": "tflops"}}},
        status_metrics={
            "arms": {
                "cpu": {
                    "error": "arm result file not written (arm may have failed)",
                    "exit_code": 1,
                    "elapsed_seconds": 3.0,
                }
            }
        },
        artifact_reference=None,
    )
    assert job.outcome == ComputeJobOutcome.FAILURE
    assert "cpu: arm result file not written" in (job.terminal_reason or "")


@pytest.mark.asyncio
async def test_measured_arm_still_counts_as_reported_data(db_session, monkeypatch):
    """The bookkeeping exclusion must not swallow a real per-arm measurement."""
    job = await _run_no_data_guard_job(
        db_session,
        monkeypatch,
        task_payload={"type": "research", "arms": {"cpu": {"metric": "tflops"}}},
        status_metrics={
            "arms": {"cpu": {"tflops": 41.2, "exit_code": 0, "elapsed_seconds": 9.0}}
        },
        artifact_reference=None,
    )
    assert job.outcome == ComputeJobOutcome.SUCCESS


@pytest.mark.asyncio
async def test_arm_bookkeeping_never_satisfies_a_declared_metric(
    db_session, monkeypatch
):
    """Bookkeeping is not a measurement, even when the arm did measure something.

    ``exit_code`` and ``elapsed_seconds`` are attached to EVERY arm by the
    aggregator. If they resolved as metric names, a sweep that declared either
    one would be recorded as having measured its hypothesis off a number the
    platform wrote itself. The arm here reports a genuine metric too, so the
    no-data guard is satisfied and this pins the metric-name resolver alone.
    """
    job = await _run_no_data_guard_job(
        db_session,
        monkeypatch,
        task_payload={
            "type": "research",
            "declared_metric": "exit_code",
            "arms": {"cpu": {"metric": "exit_code"}},
        },
        status_metrics={
            "arms": {"cpu": {"tflops": 41.2, "exit_code": 0, "elapsed_seconds": 9.0}}
        },
        artifact_reference=None,
    )
    assert job.outcome == ComputeJobOutcome.FAILURE
    assert "exit_code" in (job.terminal_reason or "")


@pytest.mark.asyncio
async def test_no_data_guard_skips_side_effect_job(db_session, monkeypatch):
    """A pure side-effect job (no declared metric, no arms) that exits 0 with no
    metrics and no artifacts stays SUCCESS — the guard must not false-reject it."""
    job = await _run_no_data_guard_job(
        db_session,
        monkeypatch,
        task_payload={"type": "research"},
        status_metrics=None,
        artifact_reference=None,
    )
    assert job.outcome == ComputeJobOutcome.SUCCESS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend_status,backend_message,expected_outcome,expected_substr",
    [
        (ComputeJobStatus.SUCCEEDED, None, ComputeJobOutcome.SUCCESS, "succeeded"),
        (ComputeJobStatus.SUCCEEDED, "", ComputeJobOutcome.SUCCESS, "succeeded"),
        (ComputeJobStatus.FAILED, None, ComputeJobOutcome.FAILURE, "failure"),
        (ComputeJobStatus.FAILED, "", ComputeJobOutcome.FAILURE, "failure"),
    ],
)
async def test_terminal_reason_not_stale_launch_placeholder_on_empty_backend_message(
    db_session,
    monkeypatch,
    backend_status,
    backend_message,
    expected_outcome,
    expected_substr,
):
    """A terminal poll with an empty backend message yields an honest reason.

    Regression guard: create_compute_job seeds polling_metadata["status_message"]
    with the launch-time placeholder "queued for compute-job launch". When a job
    reaches SUCCEEDED/FAILED but the backend poll returns an empty message,
    terminal_reason must NOT silently inherit that stale launch placeholder — it
    would then propagate into the durable knowledge/evidence record, recording a
    completed or failed run with a misleading "still queued" diagnostic.
    """
    from app import knowledge_completion

    project = Project(name="Empty Terminal Message", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="silent-terminal experiment",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "research"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python bench.py",
        machine_type="e2-standard-2",
        spend_cap_usd=5.0,
        deadline_seconds=300,
    )
    # Sanity: the launch-time placeholder really is seeded, so the fallback under
    # test has something stale to (wrongly) inherit.
    assert (
        (job.polling_metadata or {}).get("status_message")
        == "queued for compute-job launch"
    )
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    job.polling_metadata = meta
    await db_session.commit()

    class FakeBackend:
        def external_id_for(self, persisted_job):
            return "silent-job"

        async def launch(self, spec):
            return ComputeLaunchResult(
                external_id="silent-job",
                started_at=_utcnow() - timedelta(seconds=30),
                cost_rate_per_second=0.0,
                message="launched",
            )

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.0, measured_at=as_of)

        async def poll_status(self, persisted_job):
            # Terminal, but the backend reports an EMPTY message.
            return ComputeStatusResult(
                status=backend_status,
                exit_code=0 if backend_status == ComputeJobStatus.SUCCEEDED else 1,
                finished_at=_utcnow(),
                message=backend_message,
            )

        async def terminate(self, persisted_job, *, reason):
            raise AssertionError("terminate should not be called")

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeBackend()
    )
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    assert job.outcome == expected_outcome
    # The core assertion: an honest, outcome-derived reason — never the stale
    # launch placeholder.
    assert job.terminal_reason
    assert job.terminal_reason != "queued for compute-job launch"
    assert "queued for compute-job launch" not in (job.terminal_reason or "")
    assert expected_substr in job.terminal_reason

    # The durable knowledge/evidence record reads terminal_reason verbatim, so
    # the honest reason must reach the persisted diagnostics too.
    diag = knowledge_completion._extract_job_terminal_diagnostics(job)
    assert diag is not None
    assert diag["terminal_reason"] == job.terminal_reason
    assert "queued for compute-job launch" not in diag["terminal_reason"]


@pytest.mark.asyncio
async def test_process_compute_jobs_once_records_launch_failure_then_clears_on_success(
    db_session, monkeypatch
):
    project = Project(name="Compute Launch RPC Failure", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo ok",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    # Skip the pre-flight smoke gate — this test exercises launch-retry lifecycle.
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    job.polling_metadata = meta
    await db_session.commit()

    class FakeBackend:
        def __init__(self):
            self.launch_attempts = 0

        def external_id_for(self, persisted_job):
            return "launch-rpc-failure"

        async def launch(self, spec):
            self.launch_attempts += 1
            if self.launch_attempts == 1:
                raise RuntimeError("launch provider unavailable")
            return ComputeLaunchResult(
                external_id="launch-rpc-failure",
                started_at=_utcnow(),
                cost_rate_per_second=0.001,
                message="launched",
            )

    backend = FakeBackend()
    monkeypatch.setattr(compute_jobs, "get_compute_backend", lambda _name: backend)
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    diagnostic = job.polling_metadata["compute_rpc_diagnostic"]
    assert job.status == ComputeJobStatus.STARTING
    assert diagnostic["operation"] == "launch"
    assert diagnostic["error_type"] == "RuntimeError"
    assert "provider unavailable" in diagnostic["error_message"]
    assert diagnostic["attempt_count"] == 1
    assert diagnostic["next_retry_at"] is not None

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.RUNNING
    assert "compute_rpc_diagnostic" not in job.polling_metadata


class _RejectingBackend:
    """Backend whose launch fails the spec gate. Records calls; NEVER asserts.

    An `assert`/`raise` inside a backend method is worthless here: compute_jobs'
    ComputeLaunchSpecError handler wraps `terminate()` in a bare `except
    Exception:`, so an AssertionError raised in `terminate` is swallowed, logged,
    and the test still passes green while the property it "guards" is violated.
    Record the calls and assert in the TEST BODY, outside any handler.

    `provision_after_reject` models the refactor hazard: a future change that moves
    the env-key gate to AFTER provision_ephemeral_vm, so a billed VM exists by the
    time the spec is rejected. The handler must still leave no live VM behind.
    """

    def __init__(self, *, provision_after_reject: bool = False):
        self.launch_attempts = 0
        self.provision_after_reject = provision_after_reject
        # VMs that exist and are BILLING right now. Must be empty when the job
        # terminalizes — that is the whole property this suite is guarding.
        self.live_vms: list[str] = []
        self.terminate_calls: list[str | None] = []

    def external_id_for(self, persisted_job):
        return "launch-spec-rejected"

    async def launch(self, spec):
        self.launch_attempts += 1
        if self.provision_after_reject:
            # Pessimal ordering: the VM is up and billing before the gate fires.
            self.live_vms.append("launch-spec-rejected")
        raise ComputeLaunchSpecError(
            "compute job x: env key 'my-model' is not a valid shell identifier"
        )

    async def terminate(self, persisted_job, reason=None):
        self.terminate_calls.append(reason)
        self.live_vms.clear()
        return True


@pytest.mark.asyncio
async def test_process_compute_jobs_once_terminalizes_unretryable_launch_spec(
    db_session, monkeypatch
):
    """A ComputeLaunchSpecError terminalizes the job FAILED, naming the bad field.

    Contrast with the retry-shaped generic handler exercised by the test above: a
    bad env key can NEVER succeed on retry, so re-attempting it every monitor tick
    until provisioning_deadline_at only buys the operator a terminal reason of
    "compute job timed out while provisioning" — which does not say WHICH key was
    bad. The terminal_reason must carry the real cause.
    """
    project = Project(name="Compute Launch Spec Rejected", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo ok",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    job.polling_metadata = meta
    await db_session.commit()

    backend = _RejectingBackend()
    monkeypatch.setattr(compute_jobs, "get_compute_backend", lambda _name: backend)
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    # Terminal immediately — NOT left STARTING to re-raise until the deadline.
    assert job.status == ComputeJobStatus.FAILED
    assert job.outcome == ComputeJobOutcome.FAILURE
    # The operator-visible reason names the offending key (surface WHY).
    assert "my-model" in job.terminal_reason
    assert "shell identifier" in job.terminal_reason
    assert backend.launch_attempts == 1

    # ZERO-COST claim: today's gate raises before provision_ephemeral_vm, so no VM
    # was ever created and none is billing. Asserted in the test body, NOT inside a
    # backend method the handler's bare `except Exception:` would swallow.
    assert backend.live_vms == []
    # The handler reaps unconditionally (external_compute_id is always reserved
    # pre-launch), so terminate IS invoked — a belt-and-braces no-op here. Locked in
    # because it is the only thing that stops the post-provision case below leaking.
    assert backend.terminate_calls == ["launch_spec_rejected"]

    # A second tick must not re-launch a terminal job.
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    assert backend.launch_attempts == 1


@pytest.mark.asyncio
async def test_process_compute_jobs_once_reaps_vm_when_launch_rejects_post_provision(
    db_session, monkeypatch
):
    """No billed VM may survive a rejected launch spec — even if one got created.

    The property the whole ComputeLaunchSpecError path turns on is "no billed VM
    exists once we terminalize". Today that holds for free because the gate raises
    before provision_ephemeral_vm. This test refuses to depend on that: it hands
    the handler the pessimal backend — one that brings a GPU VM UP and only then
    rejects the spec — and demands the VM is gone anyway.

    That makes the guard non-vacuous. Delete the `backend.terminate(...)` call from
    the ComputeLaunchSpecError handler, or move the real gate after provisioning
    without one, and this test goes red with a live VM in hand.
    """
    project = Project(name="Compute Launch Spec Post Provision", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo ok",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    job.polling_metadata = meta
    await db_session.commit()

    backend = _RejectingBackend(provision_after_reject=True)
    monkeypatch.setattr(compute_jobs, "get_compute_backend", lambda _name: backend)
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.FAILED
    assert job.outcome == ComputeJobOutcome.FAILURE
    # The VM that existed when the spec was rejected has been reaped. Not "terminate
    # was called" — no VM is LEFT. A leak would show up here as a live instance id.
    assert backend.live_vms == []
    assert backend.terminate_calls == ["launch_spec_rejected"]


@pytest.mark.asyncio
async def test_compute_runtime_deadline_starts_after_launch(db_session, monkeypatch):
    project = Project(name="Compute Runtime Deadline", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 10",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=60,
    )
    # Skip the pre-flight smoke gate — this test exercises runtime-deadline lifecycle.
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    job.polling_metadata = meta
    await db_session.commit()

    terminate_reasons: list[str] = []

    class FakeBackend:
        def external_id_for(self, persisted_job):
            return "runtime-deadline-job"

        async def launch(self, spec):
            await asyncio.sleep(0.05)
            return ComputeLaunchResult(
                external_id="runtime-deadline-job",
                started_at=_utcnow(),
                cost_rate_per_second=0.001,
                message="launched",
            )

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.01, measured_at=as_of)

        async def poll_status(self, persisted_job):
            return ComputeStatusResult(status=ComputeJobStatus.RUNNING)

        async def terminate(self, persisted_job, *, reason):
            terminate_reasons.append(reason)
            return True

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeBackend()
    )
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.RUNNING
    assert terminate_reasons == []
    assert job.started_at is not None
    assert job.polling_metadata["runtime_deadline_at"] is not None
    assert job.polling_metadata["runtime_deadline_seconds"] == 60

    # Launch delay is no longer consumed by the runtime deadline: even if the
    # prelaunch wall clock is now in the past, the running job stays alive until
    # its launch-time runtime window expires.
    job.wall_clock_deadline = _utcnow() - timedelta(seconds=1)
    await db_session.commit()

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.RUNNING
    assert terminate_reasons == []

    job.started_at = _utcnow() - timedelta(seconds=61)
    job.polling_metadata = {
        **job.polling_metadata,
        "runtime_deadline_at": (_utcnow() - timedelta(seconds=1)).isoformat(),
    }
    await db_session.commit()

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await db_session.refresh(job)
    assert terminate_reasons == ["deadline"]
    assert job.status == ComputeJobStatus.TIMED_OUT
    assert job.outcome == ComputeJobOutcome.TIMED_OUT
    assert "runtime deadline" in (job.terminal_reason or "")


@pytest.mark.asyncio
async def test_process_compute_jobs_once_times_out_provisioning_before_launch(
    db_session, monkeypatch
):
    project = Project(name="Compute Provisioning Timeout", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 10",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=60,
    )
    # Expire both the wall_clock_deadline AND the provisioning_deadline_at
    # sub-deadline so the monitor detects the provisioning timeout immediately.
    # Also skip the pre-flight smoke gate — this test exercises provisioning timeout.
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    meta["provisioning_deadline_at"] = (
        _utcnow() + timedelta(milliseconds=200)
    ).isoformat()
    # Exhaust provisioning retry budget so the test still validates terminal
    # behavior. Without this the new auto-retry logic would extend the deadline
    # and keep the job STARTING instead of going TIMED_OUT.
    meta["provisioning_retry_count"] = compute_jobs.MAX_PROVISIONING_RETRY_COUNT
    job.polling_metadata = meta
    job.wall_clock_deadline = _utcnow() + timedelta(milliseconds=200)
    await db_session.commit()

    terminate_reasons: list[str] = []
    launch_started = asyncio.Event()

    class FakeBackend:
        def external_id_for(self, persisted_job):
            return "slow-launch-job"

        async def launch(self, spec):
            launch_started.set()
            await asyncio.sleep(0.5)
            return ComputeLaunchResult(
                external_id="slow-launch-job",
                started_at=_utcnow(),
                cost_rate_per_second=0.001,
                message="launched",
            )

        async def terminate(self, persisted_job, *, reason):
            terminate_reasons.append(reason)
            return True

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeBackend()
    )
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    assert launch_started.is_set()

    await db_session.refresh(job)
    assert terminate_reasons == ["provisioning"]
    assert job.status == ComputeJobStatus.TIMED_OUT
    assert job.outcome == ComputeJobOutcome.TIMED_OUT
    assert "provisioning" in (job.terminal_reason or "")
    diagnostic = job.polling_metadata["compute_rpc_diagnostic"]
    assert diagnostic["operation"] == "launch"
    assert diagnostic["reason"] == "provisioning_timeout"


@pytest.mark.asyncio
async def test_provisioning_subdeadline_fails_fast(db_session, monkeypatch):
    """A job stuck in provisioning is TIMED_OUT at the short sub-deadline, not
    after the full runtime budget. A job that reaches RUNNING is governed only
    by the full runtime deadline, not the sub-deadline."""
    project = Project(name="Provisioning Subdeadline Test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # deadline_seconds=3600 (1h) but COMPUTE_PROVISIONING_DEADLINE_SECONDS=360 (6m)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 10",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=3600,
    )

    # Verify the provisioning sub-deadline is much shorter than wall_clock_deadline.
    prov_deadline_raw = (job.polling_metadata or {}).get("provisioning_deadline_at")
    assert prov_deadline_raw is not None, (
        "provisioning_deadline_at must be in polling_metadata"
    )
    prov_deadline = datetime.fromisoformat(prov_deadline_raw)
    # Sub-deadline should be at most COMPUTE_PROVISIONING_DEADLINE_SECONDS after now,
    # while wall_clock_deadline is 3600s ahead.
    gap_seconds = (job.wall_clock_deadline - prov_deadline).total_seconds()
    assert gap_seconds > 100, (
        f"provisioning sub-deadline {prov_deadline} should be well before "
        f"wall_clock_deadline {job.wall_clock_deadline} (gap={gap_seconds:.1f}s)"
    )

    # Push the sub-deadline into the past to simulate a stalled provisioning VM.
    # Also skip the pre-flight smoke gate — this test exercises provisioning timeout.
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    meta["provisioning_deadline_at"] = (
        _utcnow() - timedelta(seconds=1)
    ).isoformat()
    # Exhaust retry budget so the test still validates terminal-at-sub-deadline.
    meta["provisioning_retry_count"] = compute_jobs.MAX_PROVISIONING_RETRY_COUNT
    job.polling_metadata = meta
    # wall_clock_deadline stays far in the future (the full runtime budget).
    assert (job.wall_clock_deadline - _utcnow()).total_seconds() > 1000
    await db_session.commit()

    terminate_reasons: list[str] = []

    class FakeBackend:
        def external_id_for(self, persisted_job):
            return "never-launched"

        async def launch(self, spec):  # pragma: no cover
            raise AssertionError(
                "launch should not be called after sub-deadline expiry"
            )

        async def terminate(self, persisted_job, *, reason):
            terminate_reasons.append(reason)
            return True

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeBackend()
    )
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    # Must be TIMED_OUT despite the full runtime deadline still being in the future.
    assert job.status == ComputeJobStatus.TIMED_OUT, (
        f"expected TIMED_OUT, got {job.status}"
    )
    assert job.outcome == ComputeJobOutcome.TIMED_OUT
    assert "provisioning" in (job.terminal_reason or "")
    assert terminate_reasons == ["provisioning"]

    # ------------------------------------------------------------------
    # Part 2: a job that reaches RUNNING is governed by the runtime deadline,
    # not the provisioning sub-deadline.
    # ------------------------------------------------------------------
    task2 = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task2)
    await db_session.commit()
    await db_session.refresh(task2)

    job2 = await compute_jobs.create_compute_job(
        db_session,
        task2,
        command="echo done",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=3600,
    )

    # Simulate a job that is already RUNNING with the sub-deadline expired.
    meta2 = dict(job2.polling_metadata or {})
    meta2["provisioning_deadline_at"] = (
        _utcnow() - timedelta(seconds=1)
    ).isoformat()
    job2.polling_metadata = meta2
    job2.status = ComputeJobStatus.RUNNING
    job2.started_at = _utcnow() - timedelta(seconds=5)
    await db_session.commit()

    poll_calls: list[str] = []

    class FakeRunningBackend:
        def external_id_for(self, persisted_job):
            return "running-job"

        async def poll_status(self, persisted_job):
            poll_calls.append("poll")
            from app.compute_backends import ComputeStatusResult

            return ComputeStatusResult(
                status=ComputeJobStatus.RUNNING,
                message="still running",
            )

        async def terminate(self, persisted_job, *, reason):
            return True

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeRunningBackend()
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job2)
    # Job should still be RUNNING — sub-deadline must not have terminated it.
    assert job2.status == ComputeJobStatus.RUNNING, (
        f"expected RUNNING (sub-deadline must not fire once RUNNING), got {job2.status}"
    )
    assert poll_calls, "backend.poll_status should have been called for the RUNNING job"


@pytest.mark.asyncio
async def test_compute_monitor_records_backend_rpc_failure_without_terminalizing(
    db_session, monkeypatch
):
    project = Project(name="Compute Poll RPC Failure", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.PARKED)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 10",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    job.status = ComputeJobStatus.RUNNING
    job.external_compute_id = "poll-status-rpc-failure"
    db_session.add(
        TaskWakeRegistration(
            task_id=task.id,
            project_id=project.id,
            wake_source_kind=compute_jobs.COMPUTE_JOB_TERMINAL_WAKE_KIND,
            wake_source_ref=str(job.id),
            wake_action=WakeAction.FINALIZE,
            deadline_at=_utcnow() + timedelta(hours=1),
        )
    )
    await db_session.commit()

    class FakeBackend:
        async def poll_status(self, persisted_job):
            raise RuntimeError("status API timeout")

        async def poll_cost(self, persisted_job, *, as_of):
            raise AssertionError("poll_cost should not run after status failure")

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeBackend()
    )
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    db_session.expire_all()
    await db_session.refresh(job)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    diagnostic = job.polling_metadata["compute_rpc_diagnostic"]
    assert job.status == ComputeJobStatus.RUNNING
    assert job.outcome is None
    assert job.completed_at is None
    assert diagnostic["operation"] == "poll_status"
    assert diagnostic["error_type"] == "RuntimeError"
    assert "timeout" in diagnostic["error_message"]
    assert diagnostic["job_status"] == ComputeJobStatus.RUNNING.value
    assert reg.delivery_state == WakeDeliveryState.PENDING


@pytest.mark.asyncio
async def test_running_job_past_deadline_terminates_when_poll_unreachable(
    db_session, monkeypatch
):
    """A RUNNING job whose backend is persistently unreachable must transition
    to TIMED_OUT once its wall_clock_deadline has passed, not stay RUNNING
    forever. The monitor fires terminate + finalizes in the same tick that
    detects the expired deadline, even though poll_status raised."""
    project = Project(name="Poll Unreachable Deadline", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.PARKED)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 10",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    job.status = ComputeJobStatus.RUNNING
    job.external_compute_id = "unreachable-backend-vm"
    # Backdate the wall_clock_deadline so it has already expired.
    job.wall_clock_deadline = _utcnow() - timedelta(seconds=1)
    db_session.add(
        TaskWakeRegistration(
            task_id=task.id,
            project_id=project.id,
            wake_source_kind=compute_jobs.COMPUTE_JOB_TERMINAL_WAKE_KIND,
            wake_source_ref=str(job.id),
            wake_action=WakeAction.FINALIZE,
            deadline_at=_utcnow() + timedelta(hours=1),
        )
    )
    await db_session.commit()

    terminate_reasons: list[str] = []

    class FakeBackend:
        async def poll_status(self, persisted_job):
            raise RuntimeError("backend unreachable")

        async def poll_cost(self, persisted_job, *, as_of):
            raise AssertionError("poll_cost should not run when deadline has passed")

        async def terminate(self, persisted_job, *, reason):
            terminate_reasons.append(reason)
            return True

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeBackend()
    )
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    db_session.expire_all()
    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.TIMED_OUT
    assert job.outcome == ComputeJobOutcome.TIMED_OUT
    assert job.completed_at is not None
    assert "deadline" in (job.terminal_reason or "")
    assert terminate_reasons == ["deadline"]
    diagnostic = (job.polling_metadata or {}).get("compute_rpc_diagnostic")
    assert diagnostic is not None
    assert diagnostic["operation"] == "poll_status"
    assert diagnostic["reason"] == "poll_unreachable_at_deadline"


@pytest.mark.asyncio
async def test_process_compute_jobs_once_records_cost_failure_without_firing_wake(
    db_session, monkeypatch
):
    project = Project(name="Compute Cost RPC Failure", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.PARKED)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo done",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    job.status = ComputeJobStatus.RUNNING
    job.external_compute_id = "poll-cost-rpc-failure"
    db_session.add(
        TaskWakeRegistration(
            task_id=task.id,
            project_id=project.id,
            wake_source_kind=compute_jobs.COMPUTE_JOB_TERMINAL_WAKE_KIND,
            wake_source_ref=str(job.id),
            wake_action=WakeAction.FINALIZE,
            deadline_at=_utcnow() + timedelta(hours=1),
        )
    )
    await db_session.commit()

    class FakeBackend:
        def __init__(self):
            self.cost_attempts = 0

        async def poll_status(self, persisted_job):
            return ComputeStatusResult(
                status=ComputeJobStatus.SUCCEEDED,
                exit_code=0,
                finished_at=_utcnow(),
                message="completed",
            )

        async def poll_cost(self, persisted_job, *, as_of):
            self.cost_attempts += 1
            if self.cost_attempts == 1:
                raise RuntimeError("cost API unavailable")
            return ComputeCostResult(total_cost_usd=0.04, measured_at=as_of)

        async def terminate(self, persisted_job, *, reason):
            raise AssertionError("terminate should not be called")

    backend = FakeBackend()
    monkeypatch.setattr(compute_jobs, "get_compute_backend", lambda _name: backend)
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    db_session.expire_all()
    await db_session.refresh(job)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert job.status == ComputeJobStatus.RUNNING
    assert job.completed_at is None
    assert job.polling_metadata["compute_rpc_diagnostic"]["operation"] == "poll_cost"
    assert reg.delivery_state == WakeDeliveryState.PENDING

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.commit()
    db_session.expire_all()
    await db_session.refresh(job)
    await db_session.refresh(reg)
    assert job.status == ComputeJobStatus.SUCCEEDED
    assert job.outcome == ComputeJobOutcome.SUCCESS
    assert job.final_cost_usd == pytest.approx(0.04)
    assert "compute_rpc_diagnostic" not in job.polling_metadata
    assert reg.delivery_state == WakeDeliveryState.FIRED


@pytest.mark.asyncio
async def test_poll_failure_threshold_transitions_job_to_failed(
    db_session, monkeypatch
):
    """After COMPUTE_MAX_CONSECUTIVE_POLL_FAILURES poll_status failures the job
    is terminalized to FAILED rather than remaining RUNNING indefinitely."""
    project = Project(name="Poll Failure Threshold", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.PARKED)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 999",
        machine_type="e2-standard-2",
        spend_cap_usd=10.0,
        deadline_seconds=3600,
    )
    job.status = ComputeJobStatus.RUNNING
    job.started_at = _utcnow() - timedelta(minutes=5)
    job.external_compute_id = "poll-failure-threshold"
    db_session.add(
        TaskWakeRegistration(
            task_id=task.id,
            project_id=project.id,
            wake_source_kind=compute_jobs.COMPUTE_JOB_TERMINAL_WAKE_KIND,
            wake_source_ref=str(job.id),
            wake_action=WakeAction.FINALIZE,
            deadline_at=_utcnow() + timedelta(hours=1),
        )
    )
    await db_session.commit()

    terminate_calls = []

    class FakeBackend:
        async def poll_status(self, persisted_job):
            raise RuntimeError("backend unreachable")

        async def poll_cost(self, persisted_job, *, as_of):
            raise AssertionError("poll_cost should not run after status failure")

        async def terminate(self, persisted_job, *, reason):
            terminate_calls.append(reason)
            return True

    monkeypatch.setattr(compute_jobs, "get_compute_backend", lambda _name: FakeBackend())
    monkeypatch.setattr(compute_jobs, "COMPUTE_MAX_CONSECUTIVE_POLL_FAILURES", 3)
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    # Two failures — still below threshold, job stays RUNNING.
    for _ in range(2):
        await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    db_session.expire_all()
    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.RUNNING

    # Third failure hits the threshold — job transitions to FAILED.
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    db_session.expire_all()
    await db_session.refresh(job)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()

    assert job.status == ComputeJobStatus.FAILED
    assert job.outcome == ComputeJobOutcome.FAILURE
    assert job.completed_at is not None
    assert "consecutive poll failures" in (job.terminal_reason or "")
    assert terminate_calls, "backend.terminate should be called on safety kill"
    assert reg.delivery_state == WakeDeliveryState.FIRED


@pytest.mark.asyncio
async def test_poll_failure_wall_clock_deadline_transitions_to_timed_out(
    db_session, monkeypatch
):
    """A single poll_status failure with a past wall-clock deadline terminates
    the job as TIMED_OUT rather than waiting for the threshold."""
    project = Project(name="Poll Failure Deadline", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.PARKED)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 999",
        machine_type="e2-standard-2",
        spend_cap_usd=10.0,
        deadline_seconds=3600,
    )
    job.status = ComputeJobStatus.RUNNING
    job.started_at = _utcnow() - timedelta(hours=2)
    # Place wall_clock_deadline in the past.
    job.wall_clock_deadline = _utcnow() - timedelta(minutes=10)
    job.external_compute_id = "poll-failure-deadline"
    db_session.add(
        TaskWakeRegistration(
            task_id=task.id,
            project_id=project.id,
            wake_source_kind=compute_jobs.COMPUTE_JOB_TERMINAL_WAKE_KIND,
            wake_source_ref=str(job.id),
            wake_action=WakeAction.FINALIZE,
            deadline_at=_utcnow() + timedelta(hours=1),
        )
    )
    await db_session.commit()

    class FakeBackend:
        async def poll_status(self, persisted_job):
            raise RuntimeError("backend unreachable")

        async def poll_cost(self, persisted_job, *, as_of):
            raise AssertionError("poll_cost should not run after status failure")

        async def terminate(self, persisted_job, *, reason):
            return True

    monkeypatch.setattr(compute_jobs, "get_compute_backend", lambda _name: FakeBackend())
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    db_session.expire_all()
    await db_session.refresh(job)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()

    assert job.status == ComputeJobStatus.TIMED_OUT
    assert job.outcome == ComputeJobOutcome.TIMED_OUT
    assert job.completed_at is not None
    assert "exceeded deadline" in (job.terminal_reason or "")
    assert reg.delivery_state == WakeDeliveryState.FIRED


@pytest.mark.asyncio
async def test_poll_failure_estimated_spend_cap_transitions_to_spend_cap_exceeded(
    db_session, monkeypatch
):
    """When estimated cost (accrued + rate * elapsed) exceeds the spend cap and
    poll_status fails, the job is terminalized as SPEND_CAP_EXCEEDED."""
    project = Project(name="Poll Failure Spend Cap", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.PARKED)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 999",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=86400,  # 24h — deadline well in the future
    )
    job.status = ComputeJobStatus.RUNNING
    # Started 1 minute ago; at $0.1/s that's $6 >> $1.00 cap.
    job.started_at = _utcnow() - timedelta(minutes=1)
    job.wall_clock_deadline = _utcnow() + timedelta(hours=20)
    job.external_compute_id = "poll-failure-spend-cap"
    # Store cost_rate_per_second in polling_metadata so the helper can estimate.
    meta = dict(job.polling_metadata or {})
    meta["cost_rate_per_second"] = 0.1  # $0.1/s * 60s = $6 >> $1 cap
    job.polling_metadata = meta
    db_session.add(
        TaskWakeRegistration(
            task_id=task.id,
            project_id=project.id,
            wake_source_kind=compute_jobs.COMPUTE_JOB_TERMINAL_WAKE_KIND,
            wake_source_ref=str(job.id),
            wake_action=WakeAction.FINALIZE,
            deadline_at=_utcnow() + timedelta(hours=1),
        )
    )
    await db_session.commit()

    class FakeBackend:
        async def poll_status(self, persisted_job):
            raise RuntimeError("backend unreachable")

        async def poll_cost(self, persisted_job, *, as_of):
            raise AssertionError("poll_cost should not run after status failure")

        async def terminate(self, persisted_job, *, reason):
            return True

    monkeypatch.setattr(compute_jobs, "get_compute_backend", lambda _name: FakeBackend())
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    db_session.expire_all()
    await db_session.refresh(job)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()

    assert job.status == ComputeJobStatus.FAILED
    assert job.outcome == ComputeJobOutcome.SPEND_CAP_EXCEEDED
    assert job.completed_at is not None
    assert "spend cap" in (job.terminal_reason or "")
    assert reg.delivery_state == WakeDeliveryState.FIRED


@pytest.mark.asyncio
async def test_process_compute_jobs_once_records_terminate_failure_and_still_terminalizes(
    db_session, monkeypatch
):
    project = Project(name="Compute Terminate RPC Failure", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.PARKED)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 10",
        machine_type="e2-standard-2",
        spend_cap_usd=0.02,
        deadline_seconds=300,
    )
    job.status = ComputeJobStatus.RUNNING
    job.external_compute_id = "terminate-rpc-failure"
    db_session.add(
        TaskWakeRegistration(
            task_id=task.id,
            project_id=project.id,
            wake_source_kind=compute_jobs.COMPUTE_JOB_TERMINAL_WAKE_KIND,
            wake_source_ref=str(job.id),
            wake_action=WakeAction.FINALIZE,
            deadline_at=_utcnow() + timedelta(hours=1),
        )
    )
    await db_session.commit()

    class FakeBackend:
        async def poll_status(self, persisted_job):
            return ComputeStatusResult(status=ComputeJobStatus.RUNNING)

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.05, measured_at=as_of)

        async def terminate(self, persisted_job, *, reason):
            assert reason == "budget"
            raise RuntimeError("delete failed")

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeBackend()
    )
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    db_session.expire_all()
    await db_session.refresh(job)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    diagnostic = job.polling_metadata["compute_rpc_diagnostic"]
    assert job.status == ComputeJobStatus.FAILED
    assert job.outcome == ComputeJobOutcome.SPEND_CAP_EXCEEDED
    assert job.polling_metadata["terminate_failed"] is True
    assert diagnostic["operation"] == "terminate"
    assert diagnostic["reason"] == "budget"
    assert "delete failed" in diagnostic["error_message"]
    assert reg.delivery_state == WakeDeliveryState.FIRED


@pytest.mark.asyncio
async def test_kill_compute_job_records_terminate_failure_and_returns_active_job(
    db_session, monkeypatch
):
    project = Project(name="Compute Kill RPC Failure", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 100",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    job.status = ComputeJobStatus.RUNNING
    job.external_compute_id = "operator-terminate-rpc-failure"
    db_session.add(job)
    await db_session.commit()

    class FakeBackend:
        async def terminate(self, persisted_job, *, reason):
            assert reason == "operator"
            raise RuntimeError("operator delete failed")

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeBackend()
    )

    killed = await compute_jobs.kill_compute_job(
        db_session,
        job_id=job.id,
        project_id=project.id,
        killed_by="operator@example.com",
    )

    assert killed is not None
    assert killed.status == ComputeJobStatus.RUNNING
    assert killed.outcome is None
    diagnostic = killed.polling_metadata["compute_rpc_diagnostic"]
    assert diagnostic["operation"] == "terminate"
    assert diagnostic["reason"] == "operator"
    assert "operator delete failed" in diagnostic["error_message"]


@pytest.mark.asyncio
async def test_compute_job_captures_bounded_log_artifact(db_session, monkeypatch):
    """End-to-end (local backend): a job writes durable output and emits
    stdout/stderr, and the monitor links that surviving location as a
    first-class result with a bounded command log.

    Proves the whole contract in one pass: the file is re-readable from the
    durable location AFTER the subprocess (the local analogue of VM teardown) is
    gone, and the persisted ``Artifact`` row, ``ComputeJob.artifact_id`` and
    ``ComputeJob.result_reference`` all agree on that location.
    """
    # Drive the real local-subprocess backend through the create+monitor path.
    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    project = Project(name="Compute Artifact Link", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute parent",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "declared_metric": "score"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command=(
            'printf "stdout-line\\n"; '
            'printf "stderr-line\\n" >&2; '
            'printf \'{"score":1.0}\' > "$BLUEPRINT_COMPUTE_RESULT_PATH"; '
            'printf "checkpoint-bytes" > "$BLUEPRINT_ARTIFACT_DIR/model.ckpt"'
        ),
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name=compute_backends_module.LOCAL_BACKEND_NAME,
    )
    assert job.status == ComputeJobStatus.QUEUED
    # Skip smoke gate — command writes to $BLUEPRINT_ARTIFACT_DIR which is unset
    # in the smoke environment; bypass to test full local backend lifecycle.
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    job.polling_metadata = meta
    await db_session.commit()

    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    # Pass 1 launches the subprocess; subsequent passes poll it to terminal and
    # link the artifact. The command is trivial, so a handful of passes suffices.
    for _ in range(80):
        await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
        await db_session.refresh(job)
        if job.status in compute_jobs._COMPUTE_JOB_TERMINAL_STATUSES:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("compute job did not reach a terminal state")

    assert job.status == ComputeJobStatus.SUCCEEDED
    # NOTE: outcome may be FAILURE for criterion-free compute_experiment tasks
    #       (no declared metric, arms, or success_criterion) — this test is about
    #       artifact capture, not outcome semantics.
    assert job.outcome in (ComputeJobOutcome.SUCCESS, ComputeJobOutcome.FAILURE)

    # The durable artifact directory outlived the (now-exited) subprocess and the
    # checkpoint is still re-readable from the persisted location.
    external_id = job.external_compute_id
    artifact_dir = compute_backends_module._local_artifact_dir_for_external_id(
        external_id
    )
    assert not compute_backends_module._is_pid_running(int(job.polling_metadata["pid"]))
    with open(os.path.join(artifact_dir, "model.ckpt"), encoding="utf-8") as handle:
        assert handle.read() == "checkpoint-bytes"

    durable_uri = f"file://{artifact_dir}"

    # The job is now linked to one first-class Artifact row, and all three
    # references agree on the durable location.
    assert job.artifact_id is not None
    assert job.result_reference == durable_uri

    artifact = await db_session.get(Artifact, job.artifact_id)
    assert artifact is not None
    assert artifact.task_id == task.id
    assert artifact.artifact_type == ArtifactType.COMPUTE_RESULT
    assert artifact.storage_uri == durable_uri
    assert job.polling_metadata["command_log"]["truncated"] is False

    # The artifact cites the durable result (no parallel persistence channel).
    logged = json.loads(artifact.logs)
    assert logged["compute_job_id"] == str(job.id)
    assert logged["artifact_uri"] == durable_uri
    assert logged["file_count"] >= 1
    assert logged["command_log"]["truncated"] is False
    assert "stdout-line" in logged["command_log"]["text"]
    assert "stderr-line" in logged["command_log"]["text"]


@pytest.mark.asyncio
async def test_compute_job_truncates_bounded_log_without_blocking_completion(
    db_session, monkeypatch
):
    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    project = Project(name="Compute Noisy Log", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute parent",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command=(
            "python3 - <<'PY'\n"
            "import os, sys\n"
            "sys.stdout.write('x' * 100000)\n"
            "sys.stdout.write('\\n')\n"
            "sys.stderr.write('y' * 100000)\n"
            "sys.stderr.write('\\n')\n"
            "path = os.path.join(os.environ['BLUEPRINT_ARTIFACT_DIR'], 'model.ckpt')\n"
            "with open(path, 'w', encoding='utf-8') as handle:\n"
            "    handle.write('checkpoint')\n"
            "PY"
        ),
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name=compute_backends_module.LOCAL_BACKEND_NAME,
    )
    # Skip smoke gate — command uses $BLUEPRINT_ARTIFACT_DIR (unset in smoke env).
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    job.polling_metadata = meta
    await db_session.commit()

    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    for _ in range(80):
        await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
        await db_session.refresh(job)
        if job.status in compute_jobs._COMPUTE_JOB_TERMINAL_STATUSES:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("compute job did not reach a terminal state")

    assert job.status == ComputeJobStatus.SUCCEEDED
    artifact = await db_session.get(Artifact, job.artifact_id)
    assert artifact is not None
    logged = json.loads(artifact.logs)
    assert logged["command_log"]["truncated"] is True
    assert (
        logged["command_log"]["byte_count"]
        <= compute_backends_module._MAX_COMMAND_LOG_BYTES
    )
    assert (
        len(logged["command_log"]["text"].encode("utf-8"))
        <= compute_backends_module._MAX_COMMAND_LOG_BYTES
    )


@pytest.mark.asyncio
async def test_terminal_job_with_empty_artifact_dir_links_no_artifact(
    db_session, monkeypatch
):
    """Fail-soft: a job that writes NOTHING to the artifact dir completes with no
    Artifact row and no stamped linkage — an empty location is not a result."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    project = Project(name="Compute Empty Artifact", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute parent",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="true",  # writes nothing to $BLUEPRINT_ARTIFACT_DIR or stdout/stderr
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name=compute_backends_module.LOCAL_BACKEND_NAME,
    )

    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    for _ in range(80):
        await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
        await db_session.refresh(job)
        if job.status in compute_jobs._COMPUTE_JOB_TERMINAL_STATUSES:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("compute job did not reach a terminal state")

    assert job.status == ComputeJobStatus.SUCCEEDED
    assert job.artifact_id is None
    assert job.result_reference is None
    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == task.id)))
        .scalars()
        .all()
    )
    assert artifacts == []


@pytest.mark.asyncio
async def test_create_compute_job_reuses_matching_active_job_for_task(db_session):
    project = Project(name="Compute Job Idempotent Create", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    first = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo idempotent",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    second = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo idempotent",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )

    jobs = (await db_session.execute(select(ComputeJob))).scalars().all()
    assert first.id == second.id
    assert len(jobs) == 1


@pytest.mark.asyncio
async def test_live_proof_compute_job_requires_promoted_release(db_session):
    project = Project(name="Live Proof No Promotion", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc:
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="echo live",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
            live_proof_marker="v8-live-proof",
        )

    assert exc.value.detail["reason"] == "no_promoted_release"
    jobs = (await db_session.execute(select(ComputeJob))).scalars().all()
    assert jobs == []


@pytest.mark.asyncio
async def test_ordinary_compute_job_ignores_unconfirmed_release(db_session):
    project = Project(name="Ordinary Before Serving", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="promoted",
        promoted_sha="abc123",
        promoted_at=_utcnow(),
    )
    db_session.add(version)
    await db_session.flush()
    db_session.add(
        ReleaseDeployArtifact(
            version_id=version.id,
            artifact_kind="backend_revision",
            serving_status="unconfirmed",
        )
    )
    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo ordinary",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )

    assert job.status == ComputeJobStatus.QUEUED
    assert "live_release_evidence" not in job.polling_metadata


@pytest.mark.asyncio
async def test_live_proof_compute_job_records_serving_evidence_and_idempotency(
    db_session,
):
    project = Project(name="Live Proof Serving", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    served_at = _utcnow()
    version = ReleaseVersion(
        project_id=project.id,
        version_number=7,
        status="promoted",
        promoted_sha="def456",
        promoted_at=served_at,
        serving_status="serving_confirmed",
        served_sha="def456",
        served_at=served_at,
    )
    db_session.add(version)
    await db_session.flush()
    db_session.add_all(
        [
            ReleaseDeployArtifact(
                version_id=version.id,
                artifact_kind="backend_revision",
                serving_status="serving_confirmed",
                served_identifier="def456",
                served_at=served_at,
            ),
            ReleaseDeployArtifact(
                version_id=version.id,
                artifact_kind="agent_image",
                serving_status="serving_confirmed",
                served_identifier="def456",
                served_at=served_at,
            ),
        ]
    )
    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    ordinary = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo proof",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    with pytest.raises(ValueError, match="already has an active compute job"):
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="echo proof",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
            live_proof_marker="v8-live-proof",
        )
    ordinary.status = ComputeJobStatus.SUCCEEDED
    db_session.add(ordinary)
    await db_session.commit()

    live = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo proof",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        live_proof_marker="v8-live-proof",
    )
    reused = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo proof",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        live_proof_marker="v8-live-proof",
    )

    evidence = reused.polling_metadata["live_release_evidence"]
    assert live.id == reused.id
    assert evidence["release_version_number"] == 7
    assert evidence["promoted_sha"] == "def456"
    assert evidence["aggregate_serving_status"] == "serving_confirmed"
    # agent_image is operator-visible but deliberately non-required for the
    # aggregate serving decision; promotion_merge is vacuous without GitHub
    # configuration. The backend revision is the required evidence here.
    assert {"backend_revision"}.issubset(
        {artifact["artifact_kind"] for artifact in evidence["required_artifacts"]}
    )



@pytest.mark.asyncio
async def test_create_compute_job_accepts_prices_persists_and_forwards_gpu(
    db_session, monkeypatch
):
    """A GPU accelerator request is accepted, priced (base+GPU), persisted on the
    row, and forwarded into the launch spec the GCP backend hands to the
    provisioner."""
    from app.vm_cost_rates import (
        get_compute_rate_per_second,
        get_vm_rate_per_second,
    )

    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    task = await _make_compute_task(db_session)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        machine_type="n1-standard-1",
        spend_cap_usd=5.0,
        deadline_seconds=600,
        backend_name="gcp",
        accelerator="T4",
    )

    # Persisted on the row.
    assert job.accelerator_type == "T4"
    assert job.accelerator_count == 1

    # Priced base (n1-standard-1) + GPU surcharge (T4): the persisted rate matches
    # the accelerator-aware accessor and exceeds the bare base-machine rate.
    expected_rate = get_compute_rate_per_second(
        machine_type="n1-standard-1", accelerator="T4", count=1, spot=False
    )
    assert job.polling_metadata["cost_rate_per_second"] == pytest.approx(expected_rate)
    base_only = get_vm_rate_per_second(machine_type="n1-standard-1", spot=False)
    assert expected_rate > base_only  # GPU surcharge is included

    # Forwarded into the launch spec (which GcpComputeBackend.launch passes to
    # provision_ephemeral_vm → _provision_gcp).
    spec = compute_jobs._build_launch_spec(job)
    assert spec.accelerator == "T4"
    assert spec.accelerator_count == 1
    assert spec.machine_type == "n1-standard-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("accelerator", "expected_machine_type"),
    [
        ("T4", "n1-standard-1"),
        ("L4", "g2-standard-4"),
        ("V100", "n1-standard-1"),
    ],
)
async def test_accelerator_only_resolves_canonical_machine_type(
    db_session, monkeypatch, accelerator, expected_machine_type
):
    """An accelerator-only request persists and prices the canonical GPU shape."""
    from app.routers.agent_compute_jobs import LaunchComputeJobRequest
    from app.vm_cost_rates import get_compute_rate_per_second

    request = LaunchComputeJobRequest(
        command="python train.py",
        spend_cap_usd=5.0,
        deadline_seconds=600,
        backend_name="gcp",
        accelerator=accelerator,
    )
    assert request.machine_type is None

    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    task = await _make_compute_task(db_session)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        spend_cap_usd=5.0,
        deadline_seconds=600,
        backend_name="gcp",
        accelerator=accelerator,
    )

    assert job.machine_type == expected_machine_type
    assert job.accelerator_type == accelerator
    assert job.accelerator_count == 1
    expected_rate = get_compute_rate_per_second(
        machine_type=None,
        accelerator=accelerator,
        count=1,
        spot=False,
    )
    assert job.polling_metadata["cost_rate_per_second"] == pytest.approx(expected_rate)

    spec = compute_jobs._build_launch_spec(job)
    assert spec.machine_type == expected_machine_type
    assert spec.accelerator == accelerator
    assert spec.accelerator_count == 1


@pytest.mark.asyncio
async def test_create_compute_job_gpu_extends_wall_clock_deadline(
    db_session, monkeypatch
):
    """GPU jobs add GPU_PROVISIONING_ALLOWANCE_SECONDS to wall_clock_deadline; CPU jobs do not."""
    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    task_gpu = await _make_compute_task(db_session)
    before = _utcnow()
    job_gpu = await compute_jobs.create_compute_job(
        db_session,
        task_gpu,
        command="python train.py",
        machine_type="n1-standard-1",
        spend_cap_usd=5.0,
        deadline_seconds=600,
        backend_name="gcp",
        accelerator="T4",
    )
    after = _utcnow()

    # GPU wall_clock_deadline includes the provisioning allowance.
    expected_allowance = compute_jobs.GPU_PROVISIONING_ALLOWANCE_SECONDS
    lower = before + timedelta(seconds=600 + expected_allowance)
    upper = after + timedelta(seconds=600 + expected_allowance)
    assert lower <= job_gpu.wall_clock_deadline <= upper, (
        f"GPU wall_clock_deadline {job_gpu.wall_clock_deadline} not in "
        f"[{lower}, {upper}]"
    )

    # CPU job wall_clock_deadline does NOT include the allowance.
    task_cpu = await _make_compute_task(db_session)
    before_cpu = _utcnow()
    job_cpu = await compute_jobs.create_compute_job(
        db_session,
        task_cpu,
        command="echo cpu",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=600,
    )
    after_cpu = _utcnow()
    lower_cpu = before_cpu + timedelta(seconds=600)
    upper_cpu = after_cpu + timedelta(seconds=600)
    assert lower_cpu <= job_cpu.wall_clock_deadline <= upper_cpu, (
        f"CPU wall_clock_deadline {job_cpu.wall_clock_deadline} not in "
        f"[{lower_cpu}, {upper_cpu}]"
    )


@pytest.mark.asyncio
async def test_create_compute_job_stores_park_deadline_seconds(db_session, monkeypatch):
    """park_deadline_seconds in polling_metadata equals deadline + allowance + grace."""
    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    task_gpu = await _make_compute_task(db_session)
    job_gpu = await compute_jobs.create_compute_job(
        db_session,
        task_gpu,
        command="python train.py",
        machine_type="n1-standard-1",
        spend_cap_usd=5.0,
        deadline_seconds=600,
        backend_name="gcp",
        accelerator="T4",
    )
    expected_gpu_park = (
        600
        + compute_jobs.GPU_PROVISIONING_ALLOWANCE_SECONDS
        + compute_jobs._COMPUTE_PARK_GRACE_SECONDS
    )
    assert job_gpu.polling_metadata["park_deadline_seconds"] == expected_gpu_park

    # CPU: no provisioning allowance, only grace.
    task_cpu = await _make_compute_task(db_session)
    job_cpu = await compute_jobs.create_compute_job(
        db_session,
        task_cpu,
        command="echo cpu",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=600,
    )
    expected_cpu_park = 600 + compute_jobs._COMPUTE_PARK_GRACE_SECONDS
    assert job_cpu.polling_metadata["park_deadline_seconds"] == expected_cpu_park


@pytest.mark.asyncio
async def test_create_compute_job_rejects_a100(db_session, monkeypatch):
    """An A100 (over-quota, count=0) request is rejected loudly at create time."""
    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    task = await _make_compute_task(db_session)
    with pytest.raises(ValueError, match="unsupported accelerator"):
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py",
            machine_type="n1-standard-1",
            spend_cap_usd=5.0,
            deadline_seconds=600,
            backend_name="gcp",
            accelerator="A100",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("accelerator", "machine_type"),
    [
        ("T4", "g2-standard-4"),
        ("L4", "n1-standard-1"),
    ],
)
async def test_create_compute_job_rejects_gpu_machine_family_mismatch(
    db_session, monkeypatch, accelerator, machine_type
):
    """A GPU paired with a mismatched explicit machine family is rejected."""
    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    task = await _make_compute_task(db_session)
    with pytest.raises(ValueError, match="not a valid single-GPU shape"):
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py",
            machine_type=machine_type,
            spend_cap_usd=5.0,
            deadline_seconds=600,
            backend_name="gcp",
            accelerator=accelerator,
        )


@pytest.mark.asyncio
async def test_create_compute_job_rejects_gpu_on_local_backend(db_session, monkeypatch):
    """A GPU can never be provisioned by the local-subprocess backend."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    task = await _make_compute_task(db_session)
    with pytest.raises(ValueError, match="local-subprocess"):
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py",
            machine_type="n1-standard-1",
            spend_cap_usd=5.0,
            deadline_seconds=600,
            backend_name="local-subprocess",
            accelerator="T4",
        )


@pytest.mark.asyncio
async def test_create_compute_job_selects_local_gpu_when_present(
    db_session, monkeypatch
):
    """GPU job with no explicit backend selects local-gpu when a GPU is detected."""
    from app import compute_backends as cb

    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    monkeypatch.setattr(cb, "detect_local_gpu", lambda: {"name": "Tesla T4", "memory_mib": 15360})
    monkeypatch.setattr(compute_jobs, "detect_local_gpu", lambda: {"name": "Tesla T4", "memory_mib": 15360})
    task = await _make_compute_task(db_session)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        machine_type="n1-standard-1",
        spend_cap_usd=5.0,
        deadline_seconds=600,
        accelerator="T4",
    )
    assert job.backend == "local-gpu"


@pytest.mark.asyncio
async def test_create_compute_job_selects_gcp_when_no_gpu(db_session, monkeypatch):
    """GPU job with no explicit backend auto-routes to gcp when no local GPU is detected."""
    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    monkeypatch.setattr(compute_jobs, "detect_local_gpu", lambda: None)
    task = await _make_compute_task(db_session)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        machine_type="n1-standard-1",
        spend_cap_usd=5.0,
        deadline_seconds=600,
        accelerator="T4",
    )
    assert job.backend == "gcp"


async def _make_runner_pinned_compute_task(db_session):
    """A compute_experiment task pinned to a (fictional) registered runner."""
    project = Project(name="Pinned GPU Compute Job", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
        context={
            "runner_target_id": str(uuid.uuid4()),
            "runner_hostname": "hamsterwheel",
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_create_compute_job_runner_pinned_accepts_noncatalog_accelerator(
    db_session, monkeypatch
):
    """A runner-pinned job with an operator-hardware GPU name (not in the GCP
    catalog) launches on the runner-subprocess backend with the raw name."""
    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    # No local GPU on the orchestrator host: if the pin did not drive backend
    # prediction, the job would route to gcp and be catalog-rejected.
    monkeypatch.setattr(compute_jobs, "detect_local_gpu", lambda: None)
    task = await _make_runner_pinned_compute_task(db_session)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        spend_cap_usd=5.0,
        deadline_seconds=600,
        accelerator="RTX 5080",
    )
    assert job.backend == RUNNER_SUBPROCESS_BACKEND_NAME
    assert job.accelerator_type == "RTX 5080"
    assert job.accelerator_count == 1
    # Operator hardware accrues no cloud cost — the GPU surcharge path is never
    # consulted for a non-catalog name.
    assert job.polling_metadata["cost_rate_per_second"] == 0.0


@pytest.mark.asyncio
async def test_create_compute_job_runner_pinned_trims_noncatalog_accelerator(
    db_session, monkeypatch
):
    """Whitespace around a non-catalog accelerator name is trimmed on persist."""
    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    monkeypatch.setattr(compute_jobs, "detect_local_gpu", lambda: None)
    task = await _make_runner_pinned_compute_task(db_session)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        spend_cap_usd=5.0,
        deadline_seconds=600,
        accelerator="  RTX 5080  ",
    )
    assert job.accelerator_type == "RTX 5080"


@pytest.mark.asyncio
async def test_create_compute_job_runner_pinned_canonicalizes_catalog_accelerator(
    db_session, monkeypatch
):
    """A runner-pinned job whose accelerator DOES resolve in the catalog keeps
    today's canonicalization (name and machine-type pairing)."""
    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    monkeypatch.setattr(compute_jobs, "detect_local_gpu", lambda: None)
    task = await _make_runner_pinned_compute_task(db_session)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        spend_cap_usd=5.0,
        deadline_seconds=600,
        accelerator="nvidia-tesla-v100",
    )
    assert job.backend == RUNNER_SUBPROCESS_BACKEND_NAME
    assert job.accelerator_type == "V100"
    assert job.machine_type == "n1-standard-1"
    assert job.polling_metadata["cost_rate_per_second"] == 0.0


@pytest.mark.asyncio
async def test_create_compute_job_gcp_fallback_still_rejects_noncatalog_accelerator(
    db_session, monkeypatch
):
    """No pin, no explicit backend, no local GPU → gcp routing keeps the strict
    catalog gate for a non-catalog accelerator name."""
    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    monkeypatch.setattr(compute_jobs, "detect_local_gpu", lambda: None)
    task = await _make_compute_task(db_session)
    with pytest.raises(ValueError, match=r"unsupported accelerator 'RTX 5080'"):
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py",
            spend_cap_usd=5.0,
            deadline_seconds=600,
            accelerator="RTX 5080",
        )


@pytest.mark.asyncio
async def test_create_compute_job_explicit_gcp_rejects_noncatalog_accelerator(
    db_session, monkeypatch
):
    """backend_name='gcp' + non-catalog accelerator is rejected loudly."""
    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    task = await _make_compute_task(db_session)
    with pytest.raises(ValueError, match=r"unsupported accelerator 'RTX 5080'"):
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py",
            spend_cap_usd=5.0,
            deadline_seconds=600,
            backend_name="gcp",
            accelerator="RTX 5080",
        )


@pytest.mark.asyncio
async def test_create_compute_job_local_gpu_backend_accepts_noncatalog_accelerator(
    db_session, monkeypatch
):
    """Explicit local-gpu backend accepts a non-catalog accelerator name at $0."""
    from app import compute_backends as cb

    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    gpu = {"name": "NVIDIA GeForce RTX 5080", "memory_mib": 16384}
    monkeypatch.setattr(cb, "detect_local_gpu", lambda: gpu)
    monkeypatch.setattr(compute_jobs, "detect_local_gpu", lambda: gpu)
    task = await _make_compute_task(db_session)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        spend_cap_usd=5.0,
        deadline_seconds=600,
        backend_name="local-gpu",
        accelerator="RTX 5080",
    )
    assert job.backend == "local-gpu"
    assert job.accelerator_type == "RTX 5080"
    assert job.polling_metadata["cost_rate_per_second"] == 0.0


@pytest.mark.asyncio
async def test_create_compute_job_rejects_multi_gpu_on_every_backend(
    db_session, monkeypatch
):
    """count != 1 is rejected on every backend, catalog and non-catalog alike."""
    from app import compute_backends as cb

    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    gpu = {"name": "NVIDIA GeForce RTX 5080", "memory_mib": 16384}
    monkeypatch.setattr(cb, "detect_local_gpu", lambda: gpu)
    monkeypatch.setattr(compute_jobs, "detect_local_gpu", lambda: gpu)

    # Runner-pinned, non-catalog name.
    pinned = await _make_runner_pinned_compute_task(db_session)
    with pytest.raises(ValueError, match="only single-GPU"):
        await compute_jobs.create_compute_job(
            db_session,
            pinned,
            command="python train.py",
            spend_cap_usd=5.0,
            deadline_seconds=600,
            accelerator="RTX 5080",
            accelerator_count=2,
        )

    # Explicit local-gpu backend, non-catalog name.
    task_local = await _make_compute_task(db_session)
    with pytest.raises(ValueError, match="only single-GPU"):
        await compute_jobs.create_compute_job(
            db_session,
            task_local,
            command="python train.py",
            spend_cap_usd=5.0,
            deadline_seconds=600,
            backend_name="local-gpu",
            accelerator="RTX 5080",
            accelerator_count=2,
        )

    # GCP, catalog name.
    task_gcp = await _make_compute_task(db_session)
    with pytest.raises(ValueError, match="only single-GPU"):
        await compute_jobs.create_compute_job(
            db_session,
            task_gcp,
            command="python train.py",
            spend_cap_usd=5.0,
            deadline_seconds=600,
            backend_name="gcp",
            accelerator="T4",
            accelerator_count=2,
        )


@pytest.mark.asyncio
async def test_local_subprocess_still_rejected_for_gpu(db_session, monkeypatch):
    """Explicitly requesting local-subprocess with a GPU job raises ValueError."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    task = await _make_compute_task(db_session)
    with pytest.raises(ValueError, match="local-subprocess"):
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py",
            machine_type="n1-standard-1",
            spend_cap_usd=5.0,
            deadline_seconds=600,
            backend_name="local-subprocess",
            accelerator="T4",
        )


@pytest.mark.asyncio
async def test_operator_local_backends_accrue_zero_cost(db_session, monkeypatch):
    """local-subprocess and local-gpu backends persist cost_rate_per_second=0.0,
    while gcp retains a positive billed rate.  _accrued_cost stays $0 for local runs."""
    from app import compute_backends as cb
    from app.compute_backends import _accrued_cost

    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    task_local = await _make_compute_task(db_session)

    # local-subprocess: rate must be exactly 0.0
    job_local = await compute_jobs.create_compute_job(
        db_session,
        task_local,
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=5.0,
        deadline_seconds=600,
        backend_name="local-subprocess",
    )
    assert job_local.polling_metadata["cost_rate_per_second"] == 0.0

    # _accrued_cost stays $0 even after simulated elapsed time
    job_local.started_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    as_of = datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc)  # 1 hour later
    assert _accrued_cost(job_local, as_of) == 0.0

    # local-gpu: rate must also be 0.0
    monkeypatch.setattr(cb, "detect_local_gpu", lambda: {"name": "Tesla T4", "memory_mib": 15360})
    monkeypatch.setattr(compute_jobs, "detect_local_gpu", lambda: {"name": "Tesla T4", "memory_mib": 15360})
    task_gpu = await _make_compute_task(db_session)
    job_gpu = await compute_jobs.create_compute_job(
        db_session,
        task_gpu,
        command="python train.py",
        machine_type="n1-standard-1",
        spend_cap_usd=5.0,
        deadline_seconds=600,
        backend_name="local-gpu",
        accelerator="T4",
    )
    assert job_gpu.polling_metadata["cost_rate_per_second"] == 0.0

    # gcp backend must retain a positive (billed) rate
    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    task_gcp = await _make_compute_task(db_session)
    job_gcp = await compute_jobs.create_compute_job(
        db_session,
        task_gcp,
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=5.0,
        deadline_seconds=600,
        backend_name="gcp",
    )
    assert job_gcp.polling_metadata["cost_rate_per_second"] > 0.0


@pytest.mark.asyncio
async def test_process_compute_jobs_once_kills_on_spend_cap(db_session, monkeypatch):
    project = Project(name="Compute Job Budget Kill", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 10",
        machine_type="e2-standard-2",
        spend_cap_usd=0.02,
        deadline_seconds=300,
    )
    job.status = ComputeJobStatus.RUNNING
    job.external_compute_id = "fake-job-2"
    job.started_at = _utcnow() - timedelta(seconds=60)
    db_session.add(job)
    await db_session.commit()

    terminate_reasons: list[str] = []

    class FakeBackend:
        async def launch(self, spec):
            raise AssertionError("launch should not be called")

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(
                total_cost_usd=0.05,
                measured_at=as_of,
            )

        async def poll_status(self, persisted_job):
            return ComputeStatusResult(status=ComputeJobStatus.RUNNING)

        async def terminate(self, persisted_job, *, reason):
            terminate_reasons.append(reason)
            return True

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeBackend()
    )
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    assert terminate_reasons == ["budget"]
    assert job.status == ComputeJobStatus.FAILED
    assert job.outcome == ComputeJobOutcome.SPEND_CAP_EXCEEDED
    assert "spend cap" in (job.terminal_reason or "")


@pytest.mark.asyncio
async def test_process_compute_jobs_once_kills_on_deadline(db_session, monkeypatch):
    project = Project(name="Compute Job Deadline Kill", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 10",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=60,
    )
    job.status = ComputeJobStatus.RUNNING
    job.external_compute_id = "fake-job-3"
    job.started_at = _utcnow() - timedelta(seconds=60)
    job.wall_clock_deadline = _utcnow() + timedelta(seconds=60)
    db_session.add(job)
    await db_session.commit()

    terminate_reasons: list[str] = []

    class FakeBackend:
        async def launch(self, spec):
            raise AssertionError("launch should not be called")

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(
                total_cost_usd=0.01,
                measured_at=as_of,
            )

        async def poll_status(self, persisted_job):
            return ComputeStatusResult(status=ComputeJobStatus.RUNNING)

        async def terminate(self, persisted_job, *, reason):
            terminate_reasons.append(reason)
            return True

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeBackend()
    )
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    assert terminate_reasons == ["deadline"]
    assert job.status == ComputeJobStatus.TIMED_OUT
    assert job.outcome == ComputeJobOutcome.TIMED_OUT
    assert "runtime deadline" in (job.terminal_reason or "")


@pytest.mark.asyncio
async def test_kill_compute_job_cancels_active_and_terminates(db_session, monkeypatch):
    project = Project(name="Compute Job Operator Kill", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 100",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    job.status = ComputeJobStatus.RUNNING
    job.external_compute_id = "fake-job-kill"
    db_session.add(job)
    await db_session.commit()

    terminate_reasons: list[str] = []

    class FakeBackend:
        async def terminate(self, persisted_job, *, reason):
            terminate_reasons.append(reason)
            return True

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeBackend()
    )

    killed = await compute_jobs.kill_compute_job(
        db_session,
        job_id=job.id,
        project_id=project.id,
        killed_by="operator@example.com",
    )

    assert terminate_reasons == ["operator"]
    assert killed.status == ComputeJobStatus.CANCELLED
    assert killed.outcome == ComputeJobOutcome.CANCELLED
    assert "operator" in (killed.terminal_reason or "")
    assert "operator@example.com" in (killed.terminal_reason or "")
    assert killed.final_cost_usd == killed.accrued_cost_usd


@pytest.mark.asyncio
async def test_kill_compute_job_rejects_terminal_job(db_session):
    project = Project(name="Compute Job Kill Terminal", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo done",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    job.status = ComputeJobStatus.SUCCEEDED
    db_session.add(job)
    await db_session.commit()

    with pytest.raises(ValueError):
        await compute_jobs.kill_compute_job(
            db_session, job_id=job.id, project_id=project.id
        )


@pytest.mark.asyncio
async def test_kill_compute_job_rejects_wrong_project(db_session):
    project = Project(name="Compute Job Kill Scope A", user_id="test-user")
    other = Project(name="Compute Job Kill Scope B", user_id="test-user")
    db_session.add_all([project, other])
    await db_session.commit()
    await db_session.refresh(project)
    await db_session.refresh(other)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="sleep 100",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )

    with pytest.raises(ValueError):
        await compute_jobs.kill_compute_job(
            db_session, job_id=job.id, project_id=other.id
        )


@pytest.mark.asyncio
async def test_list_compute_jobs_returns_active_and_recent_excludes_others(db_session):
    project = Project(name="Compute Job List", user_id="test-user")
    other = Project(name="Compute Job List Other", user_id="test-user")
    db_session.add_all([project, other])
    await db_session.commit()
    await db_session.refresh(project)
    await db_session.refresh(other)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    other_task = Task(project_id=other.id, status=TaskStatus.IN_PROGRESS)
    db_session.add_all([task, other_task])
    await db_session.commit()
    await db_session.refresh(task)
    await db_session.refresh(other_task)

    now = _utcnow()

    active = ComputeJob(
        task_id=task.id,
        project_id=project.id,
        backend="local-subprocess",
        status=ComputeJobStatus.RUNNING,
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        accrued_cost_usd=0.1,
    )
    recent_done = ComputeJob(
        task_id=task.id,
        project_id=project.id,
        backend="local-subprocess",
        status=ComputeJobStatus.SUCCEEDED,
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        completed_at=now - timedelta(minutes=10),
    )
    stale_done = ComputeJob(
        task_id=task.id,
        project_id=project.id,
        backend="local-subprocess",
        status=ComputeJobStatus.SUCCEEDED,
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        completed_at=now - timedelta(hours=48),
    )
    other_active = ComputeJob(
        task_id=other_task.id,
        project_id=other.id,
        backend="local-subprocess",
        status=ComputeJobStatus.RUNNING,
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
    )
    db_session.add_all([active, recent_done, stale_done, other_active])
    await db_session.commit()

    jobs, has_more = await compute_jobs.list_compute_jobs(
        db_session, project_id=project.id, include_finished_within=timedelta(hours=6)
    )
    assert has_more is False
    job_ids = {j.id for j in jobs}
    assert active.id in job_ids
    assert recent_done.id in job_ids
    assert stale_done.id not in job_ids  # finished outside the window
    assert other_active.id not in job_ids  # different project


@pytest.mark.asyncio
async def test_list_compute_jobs_applies_limit_and_offset(db_session):
    project = Project(name="Paged Compute", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    jobs = []
    for _ in range(3):
        job = ComputeJob(
            task_id=task.id,
            project_id=project.id,
            backend="local-subprocess",
            status=ComputeJobStatus.RUNNING,
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
        jobs.append(job)

    page, has_more = await compute_jobs.list_compute_jobs(
        db_session,
        project_id=project.id,
        include_finished_within=timedelta(hours=6),
        limit=2,
        offset=1,
    )

    assert has_more is False
    assert [job.id for job in page] == [jobs[1].id, jobs[0].id]


@pytest.mark.asyncio
async def test_get_recent_terminal_compute_spend_terminal_only_and_windowed(db_session):
    """Helper sums final_cost_usd for terminal, costed jobs in [since, until)."""
    project = Project(name="Compute Spend Window", user_id="test-user")
    other = Project(name="Other Project", user_id="test-user")
    db_session.add_all([project, other])
    await db_session.commit()
    await db_session.refresh(project)
    await db_session.refresh(other)

    today_start = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    def _job(*, proj_id, status, final_cost, completed_at):
        return ComputeJob(
            project_id=proj_id,
            backend="local",
            machine_type="e2-standard-2",
            status=status,
            final_cost_usd=final_cost,
            accrued_cost_usd=final_cost or 0.0,
            completed_at=completed_at,
        )

    jobs = [
        # In-window terminal jobs with cost — counted.
        _job(
            proj_id=project.id,
            status=ComputeJobStatus.SUCCEEDED,
            final_cost=0.50,
            completed_at=today_start + timedelta(hours=1),
        ),
        _job(
            proj_id=project.id,
            status=ComputeJobStatus.TIMED_OUT,
            final_cost=0.25,
            completed_at=today_start + timedelta(hours=2),
        ),
        # Running job (in-flight) — ignored even though it accrued cost.
        ComputeJob(
            project_id=project.id,
            backend="local",
            machine_type="e2-standard-2",
            status=ComputeJobStatus.RUNNING,
            accrued_cost_usd=99.0,
            final_cost_usd=None,
            completed_at=None,
        ),
        # Terminal but no final_cost_usd — ignored.
        _job(
            proj_id=project.id,
            status=ComputeJobStatus.FAILED,
            final_cost=None,
            completed_at=today_start + timedelta(hours=3),
        ),
        # Before the window — ignored.
        _job(
            proj_id=project.id,
            status=ComputeJobStatus.SUCCEEDED,
            final_cost=7.0,
            completed_at=today_start - timedelta(hours=1),
        ),
        # Different project — ignored when scoped.
        _job(
            proj_id=other.id,
            status=ComputeJobStatus.SUCCEEDED,
            final_cost=4.0,
            completed_at=today_start + timedelta(hours=1),
        ),
    ]
    db_session.add_all(jobs)
    await db_session.commit()

    total = await compute_jobs.get_recent_terminal_compute_spend_usd(
        db_session, project_id=project.id, since=today_start
    )
    assert total == pytest.approx(0.75)

    # until is exclusive: a window ending before the 2h job drops it.
    windowed = await compute_jobs.get_recent_terminal_compute_spend_usd(
        db_session,
        project_id=project.id,
        since=today_start,
        until=today_start + timedelta(hours=2),
    )
    assert windowed == pytest.approx(0.50)

    # project_id=None aggregates across projects (0.75 + 4.0).
    cross = await compute_jobs.get_recent_terminal_compute_spend_usd(
        db_session, project_id=None, since=today_start
    )
    assert cross == pytest.approx(4.75)


@pytest.mark.asyncio
async def test_compute_job_terminal_wakes_parked_task_once(db_session, monkeypatch):
    """A compute_experiment task parked on its job is woken EXACTLY ONCE.

    Exercises the full park-and-wake compute flow end to end:
      1. The task launches a budgeted compute job and PARKS on a
         ``compute_job_terminal`` wake (releasing its agent VM/slot).
      2. The monitor drives the job to a terminal SUCCESS and fires the wake
         once, in the same transaction as the job finalization.
      3. The leader-owned wake-delivery sweep finalizes the parked task through
         the real compute_experiment completion path — honest outcome + cost.
      4. Duplicate monitor ticks and duplicate sweeps neither double-fire nor
         double-finalize (terminal/wake/launch idempotency).
    """
    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_lifecycle import park_task
    from app.time_utils import _utcnow

    project = Project(name="Compute Park Wake", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute_experiment: parked job",
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "compute_experiment",
                "base_branch": "main",
                "declared_metric": "score",
            }
        ],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo ok",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    # Skip smoke gate — this test exercises the park/wake lifecycle post-smoke.
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    job.polling_metadata = meta
    await db_session.commit()

    # The task parks on the launched job instead of poll-pinning its VM.
    await park_task(
        db_session,
        task,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(job.id),
        wake_action=WakeAction.FINALIZE,
        deadline_at=_utcnow() + timedelta(hours=1),
        reason=f"parked on compute job {job.id}",
    )
    await db_session.commit()
    await db_session.refresh(task)
    assert task.status == TaskStatus.PARKED
    assert task.parked_at is not None

    started_at = _utcnow() - timedelta(seconds=30)

    class FakeBackend:
        def external_id_for(self, persisted_job):
            return "park-job-1"

        async def launch(self, spec):
            return ComputeLaunchResult(
                external_id="park-job-1",
                started_at=started_at,
                cost_rate_per_second=0.001,
                message="launched",
            )

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.12, measured_at=as_of)

        async def poll_status(self, persisted_job):
            return ComputeStatusResult(
                status=ComputeJobStatus.SUCCEEDED,
                exit_code=0,
                finished_at=_utcnow(),
                message="completed",
                metrics={"score": 1.0},
            )

        async def terminate(self, persisted_job, *, reason):
            raise AssertionError("terminate should not be called")

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeBackend()
    )
    monkeypatch.setattr(
        "app.task_completion._spawn_act_on_verdict_followon",
        AsyncMock(return_value=None),
    )
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    # Tick 1 launches (QUEUED→RUNNING), tick 2 observes terminal + fires the wake.
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    db_session.expire_all()
    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.SUCCEEDED
    # criterion-free compute_experiment → outcome may be FAILURE (no metric declared)
    assert job.outcome in (ComputeJobOutcome.SUCCESS, ComputeJobOutcome.FAILURE)

    regs = (await db_session.execute(select(TaskWakeRegistration))).scalars().all()
    assert len(regs) == 1, "launch must not create duplicate parked registrations"
    assert regs[0].delivery_state == WakeDeliveryState.FIRED

    # The leader-owned wake worker finalizes the parked task exactly once.
    delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert delivered == 1

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert task.status == TaskStatus.COMPLETED
    assert task.parked_at is None
    assert reg.delivery_state == WakeDeliveryState.DELIVERED
    assert reg.result_state == "finalized"
    assert reg.attempt_count == 1
    compute_result = (task.plan_output or {}).get("compute_result")
    assert compute_result is not None
    # criterion-free compute_experiment → outcome may be FAILURE (no metric declared)
    assert compute_result["outcome"] in ("SUCCESS", "FAILURE")
    assert compute_result["final_cost_usd"] == pytest.approx(0.12)

    # Idempotency: a duplicate monitor tick skips the already-terminal job (no
    # second fire) and a duplicate sweep finds no FIRED row to deliver — the task
    # is never double-finalized and the registration is never re-delivered.
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    again = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert again == 0

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert task.status == TaskStatus.COMPLETED
    assert reg.delivery_state == WakeDeliveryState.DELIVERED
    assert reg.attempt_count == 1


@pytest.mark.asyncio
async def test_compute_job_terminal_before_park_still_finalizes_once(
    db_session, monkeypatch
):
    """Regression: a compute job that reaches terminal BEFORE the task parks must
    still wake and finalize the task EXACTLY ONCE — never a ``park_wake_timeout``.

    Forces the terminal-before-park ordering deterministically:
      1. The monitor drives the job to terminal SUCCESS and fires the wake while
         NO park registration exists yet (``fire_task_wakes`` finds nothing).
      2. The task parks on the ALREADY-TERMINAL job. ``park_task``'s
         register-then-recheck (``_maybe_fire_terminal_compute_wake``) takes the
         shared compute-job row lock, sees the terminal job, and fires the
         just-created registration (PENDING -> FIRED) so the leader sweep can
         finalize it identically to a normally-fired wake.
      3. The delivery sweep finalizes the parked task COMPLETED with the job's
         honest outcome + cost — exactly once, not FAILED, not stranded.

    Without the fix the registration would stay PENDING forever (the monitor
    skips the already-terminal job), EXPIRE at the park deadline, and FAIL the
    task with a misleading ``park_wake_timeout`` even though the job SUCCEEDED.
    """
    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_lifecycle import park_task
    from app.time_utils import _utcnow

    project = Project(name="Compute Terminal Before Park", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute_experiment: terminal before park",
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "compute_experiment",
                "base_branch": "main",
                "declared_metric": "score",
            }
        ],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo ok",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    # Skip smoke gate — this test exercises the terminal-before-park lifecycle.
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    job.polling_metadata = meta
    await db_session.commit()

    started_at = _utcnow() - timedelta(seconds=30)

    class FakeBackend:
        def external_id_for(self, persisted_job):
            return "terminal-before-park-1"

        async def launch(self, spec):
            return ComputeLaunchResult(
                external_id="terminal-before-park-1",
                started_at=started_at,
                cost_rate_per_second=0.001,
                message="launched",
            )

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.21, measured_at=as_of)

        async def poll_status(self, persisted_job):
            return ComputeStatusResult(
                status=ComputeJobStatus.SUCCEEDED,
                exit_code=0,
                finished_at=_utcnow(),
                message="completed",
                metrics={"score": 1.0},
            )

        async def terminate(self, persisted_job, *, reason):
            raise AssertionError("terminate should not be called")

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeBackend()
    )
    monkeypatch.setattr(
        "app.task_completion._spawn_act_on_verdict_followon",
        AsyncMock(return_value=None),
    )
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    # --- Drive the job to terminal with NO park registration present. --------
    # Tick 1 launches (QUEUED->RUNNING), tick 2 observes terminal + fires the
    # wake — but no registration exists, so fire_task_wakes flips nothing.
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    db_session.expire_all()
    await db_session.refresh(job)
    # Re-load the task too: park_task reads task attributes, and an expired
    # instance would lazy-load (MissingGreenlet under real asyncpg expiry).
    await db_session.refresh(task)
    assert job.status == ComputeJobStatus.SUCCEEDED
    # criterion-free compute_experiment → outcome may be FAILURE (no metric declared)
    assert job.outcome in (ComputeJobOutcome.SUCCESS, ComputeJobOutcome.FAILURE)
    # The monitor fired nothing — no registration existed when the job finalized.
    pre_regs = (await db_session.execute(select(TaskWakeRegistration))).scalars().all()
    assert pre_regs == [], "no registration should exist before the task parks"

    # --- NOW park on the already-terminal job (the lost-wakeup window). -------
    await park_task(
        db_session,
        task,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(job.id),
        wake_action=WakeAction.FINALIZE,
        deadline_at=_utcnow() + timedelta(hours=1),
        reason=f"parked on compute job {job.id}",
    )
    await db_session.commit()

    db_session.expire_all()
    await db_session.refresh(task)
    assert task.status == TaskStatus.PARKED
    # The register-then-recheck caught the already-terminal job and fired the
    # registration immediately — it must NOT be left PENDING to time out.
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert reg.delivery_state == WakeDeliveryState.FIRED, (
        "park-time terminal recheck must fire the registration for an "
        "already-terminal job"
    )
    assert reg.fired_at is not None

    # --- The leader sweep finalizes the parked task exactly once. ------------
    delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert delivered == 1

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert task.status == TaskStatus.COMPLETED
    assert task.parked_at is None
    # Honest terminal outcome + cost carried through the real completion path —
    # never a park_wake_timeout.
    assert (task.context or {}).get("failure_category") != "park_wake_timeout"
    compute_result = (task.plan_output or {}).get("compute_result")
    assert compute_result is not None
    # criterion-free compute_experiment → outcome may be FAILURE (no metric declared)
    assert compute_result["outcome"] in ("SUCCESS", "FAILURE")
    assert compute_result["final_cost_usd"] == pytest.approx(0.21)
    assert reg.delivery_state == WakeDeliveryState.DELIVERED
    assert reg.result_state == "finalized"
    assert reg.attempt_count == 1

    # --- Idempotency: a second sweep is a strict no-op. ----------------------
    first_delivered_at = reg.delivered_at
    again = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert again == 0

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert task.status == TaskStatus.COMPLETED
    assert reg.delivery_state == WakeDeliveryState.DELIVERED
    assert reg.delivered_at == first_delivered_at  # unchanged — delivered once
    assert reg.attempt_count == 1  # FIRED filter never re-selected it


@pytest.mark.asyncio
async def test_compute_wake_delivery_commit_is_durable_and_single(
    db_session, monkeypatch
):
    """BUG 2: the compute FINALIZE wake delivery commits ONCE, durably, and is a
    strict no-op on re-delivery.

    ``deliver_wake`` is now caller-committed for the compute branch too: the
    FIRED->DELIVERED flip and the task advance commit together under the sweep's
    single per-registration commit. This asserts the state change is DURABLE
    (visible from a fresh session) and that a repeated sweep neither re-advances
    the task nor re-increments ``attempt_count`` (the FIRED filter never
    re-selects a DELIVERED row).
    """
    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_lifecycle import fire_task_wakes, park_task
    from app.time_utils import _utcnow

    project = Project(name="Compute Wake Commit Contract", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute_experiment: commit contract",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo ok",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    # Park BEFORE terminal (the normal ordering): the job is still QUEUED, so the
    # park-time recheck leaves the registration PENDING.
    await park_task(
        db_session,
        task,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(job.id),
        wake_action=WakeAction.FINALIZE,
        deadline_at=_utcnow() + timedelta(hours=1),
        reason=f"parked on compute job {job.id}",
    )
    await db_session.commit()

    db_session.expire_all()
    await db_session.refresh(task)
    # Re-load the job before mutating it: writing to an expired instance makes
    # the flush lazy-load old attribute state (MissingGreenlet under asyncpg).
    await db_session.refresh(job)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert task.status == TaskStatus.PARKED
    assert reg.delivery_state == WakeDeliveryState.PENDING

    # Finalize the job and fire the wake (the firing side the monitor performs).
    job.status = ComputeJobStatus.SUCCEEDED
    job.outcome = ComputeJobOutcome.SUCCESS
    job.final_cost_usd = 0.34
    job.accrued_cost_usd = 0.34
    db_session.add(job)
    await fire_task_wakes(
        db_session,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(job.id),
    )
    await db_session.commit()

    # Deliver via the sweep (single per-registration commit owns the advance).
    monkeypatch.setattr(
        "app.task_completion._spawn_act_on_verdict_followon",
        AsyncMock(return_value=None),
    )
    delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert delivered == 1

    # Durability: the COMPLETED status + DELIVERED registration are visible from
    # a brand-new session — the advance was actually committed, not just buffered.
    fresh_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )
    async with fresh_factory() as fresh:
        fresh_task = await fresh.get(Task, task.id)
        fresh_reg = (await fresh.execute(select(TaskWakeRegistration))).scalars().one()
        assert fresh_task.status == TaskStatus.COMPLETED
        assert fresh_reg.delivery_state == WakeDeliveryState.DELIVERED
        assert fresh_reg.attempt_count == 1
        durable_delivered_at = fresh_reg.delivered_at
        assert durable_delivered_at is not None

    # Re-delivery is a strict no-op: no second advance, delivered_at unchanged,
    # attempt_count not re-incremented (the DELIVERED row is never re-selected).
    again = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert again == 0
    async with fresh_factory() as fresh:
        fresh_reg = (await fresh.execute(select(TaskWakeRegistration))).scalars().one()
        assert fresh_reg.delivery_state == WakeDeliveryState.DELIVERED
        assert fresh_reg.delivered_at == durable_delivered_at
        assert fresh_reg.attempt_count == 1


@pytest.mark.asyncio
async def test_park_task_waits_for_terminal_job_commit_before_catch_up(
    db_session, monkeypatch
):
    """park_task must wait out an in-flight terminalization before catch-up.

    If the monitor has already terminalized the compute job and fired wakes in an
    uncommitted transaction, park_task should block on the compute-job row lock and
    then create the registration as FIRED once that transaction commits.
    """
    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND
    from app.task_lifecycle import fire_task_wakes, park_task

    project = Project(name="Race Fix Project", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute_experiment: terminal race fix",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo terminal race",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )

    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    async with session_factory() as terminal_session:
        locked_job = await terminal_session.get(
            ComputeJob, job.id, with_for_update=True
        )
        assert locked_job is not None
        locked_job.status = ComputeJobStatus.SUCCEEDED
        locked_job.outcome = ComputeJobOutcome.SUCCESS
        locked_job.completed_at = _utcnow()
        locked_job.final_cost_usd = 0.25
        locked_job.accrued_cost_usd = 0.25
        await terminal_session.flush()

        fired = await fire_task_wakes(
            terminal_session,
            wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
            wake_source_ref=str(job.id),
        )
        assert fired == 0

        started = asyncio.Event()

        async def _park_after_commit():
            async with session_factory() as park_session:
                parked_task = await park_session.get(Task, task.id)
                assert parked_task is not None
                started.set()
                reg = await park_task(
                    park_session,
                    parked_task,
                    wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
                    wake_source_ref=str(job.id),
                    wake_action=WakeAction.FINALIZE,
                    reason=f"waiting on compute job {job.id}",
                )
                await park_session.commit()
                await park_session.refresh(reg)
                return reg

        parked = asyncio.create_task(_park_after_commit())
        await started.wait()
        await asyncio.sleep(0.05)
        assert not parked.done()

        await terminal_session.commit()
        reg = await parked
        reg_id = reg.id

    await db_session.refresh(task)
    reg = await db_session.get(TaskWakeRegistration, reg_id)
    assert reg is not None
    assert task.status == TaskStatus.PARKED
    assert reg.delivery_state == WakeDeliveryState.FIRED
    assert reg.fired_at is not None


@pytest.mark.asyncio
async def test_wake_post_commit_side_effects_are_recoverable(db_session, monkeypatch):
    """A transient wake post-commit failure leaves a durable retry marker.

    The first sweep delivers the wake and then fails while persisting the
    compute knowledge document. The next sweep retries the delivered wake row,
    re-attempts broadcast + knowledge persistence, and clears the marker without
    creating duplicate knowledge documents.
    """
    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND
    from app import knowledge_completion as knowledge_completion_module
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_completion import (
        _COMPUTE_WAKE_NEXT_RETRY_KEY,
        _COMPUTE_WAKE_POST_COMMIT_META_KEY,
    )
    from app.task_lifecycle import fire_task_wakes, park_task
    from app.time_utils import _utcnow

    project = Project(name="Compute Wake Recoverable Side Effects", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute_experiment: recoverable post-commit",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo ok",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    await park_task(
        db_session,
        task,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(job.id),
        wake_action=WakeAction.FINALIZE,
        deadline_at=_utcnow() + timedelta(hours=1),
        reason=f"parked on compute job {job.id}",
    )
    await db_session.commit()

    job.status = ComputeJobStatus.SUCCEEDED
    job.outcome = ComputeJobOutcome.SUCCESS
    job.final_cost_usd = 0.34
    job.accrued_cost_usd = 0.34
    db_session.add(job)
    await fire_task_wakes(
        db_session,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(job.id),
    )
    await db_session.commit()

    real_persist = knowledge_completion_module.persist_compute_experiment_knowledge
    persist_calls = 0

    async def flaky_persist(*args, **kwargs):
        nonlocal persist_calls
        persist_calls += 1
        if persist_calls == 1:
            raise RuntimeError("transient knowledge persistence failure")
        return await real_persist(*args, **kwargs)

    monkeypatch.setattr(
        "app.task_completion.broadcast_to_project",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.knowledge_completion._analyze_compute_experiment_with_llm",
        AsyncMock(return_value=(None, "precondition-not-met")),
    )
    monkeypatch.setattr(
        "app.knowledge_completion.persist_compute_experiment_knowledge",
        flaky_persist,
    )
    monkeypatch.setattr(
        "app.task_completion._spawn_act_on_verdict_followon",
        AsyncMock(return_value=None),
    )

    delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert delivered == 1

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    docs = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert task.status == TaskStatus.COMPLETED
    assert reg.delivery_state == WakeDeliveryState.DELIVERED
    assert reg.result_state == "finalized"
    assert reg.source_metadata.get(_COMPUTE_WAKE_POST_COMMIT_META_KEY) is not None
    first_next_retry = reg.source_metadata.get(_COMPUTE_WAKE_NEXT_RETRY_KEY)
    assert first_next_retry is not None
    assert docs == []
    assert persist_calls == 1

    again = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert again == 0

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    docs = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert task.status == TaskStatus.COMPLETED
    assert reg.delivery_state == WakeDeliveryState.DELIVERED
    assert reg.source_metadata.get(_COMPUTE_WAKE_POST_COMMIT_META_KEY) is not None
    assert reg.source_metadata.get(_COMPUTE_WAKE_NEXT_RETRY_KEY) == first_next_retry
    assert len(docs) == 0
    assert persist_calls == 1

    retry_at = datetime.fromisoformat(first_next_retry) + timedelta(seconds=1)
    monkeypatch.setattr("app.task_completion._utcnow", lambda: retry_at)
    monkeypatch.setattr("app.reaper_sweeps._utcnow", lambda: retry_at)

    final = await sweep_deliver_task_wakes(db_session, retry_at)
    assert final == 0

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    docs = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert task.status == TaskStatus.COMPLETED
    assert reg.delivery_state == WakeDeliveryState.DELIVERED
    assert reg.source_metadata.get(_COMPUTE_WAKE_POST_COMMIT_META_KEY) is None
    assert _COMPUTE_WAKE_NEXT_RETRY_KEY not in (reg.source_metadata or {})
    assert len(docs) == 1
    assert docs[0].source_task_id == task.id
    assert persist_calls == 2


@pytest.mark.asyncio
async def test_wake_post_commit_recoverable_when_crash_before_marker_write(
    db_session, monkeypatch
):
    """Crash between DELIVERED commit and marker write still leaves side effects recoverable.

    Under the old code, the recoverability marker was written inside
    run_compute_wake_post_commit — so a crash between the pass-1 DELIVERED commit
    and that function's first line would leave a DELIVERED row with NO marker,
    causing the retry pass to skip it forever.

    With the fix, deliver_wake seeds the marker atomically with the FIRED→DELIVERED
    commit, so even a simulated crash right after the commit (before any post-commit
    side effects run) leaves the marker in place and a subsequent sweep will
    re-attempt and persist exactly one knowledge document.
    """
    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_lifecycle import deliver_wake, fire_task_wakes, park_task
    from app.task_completion import _COMPUTE_WAKE_POST_COMMIT_META_KEY
    from app.time_utils import _utcnow

    project = Project(
        name="Compute Wake Crash Before Marker Write", user_id="test-user"
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute_experiment: crash before marker write",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo ok",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    await park_task(
        db_session,
        task,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(job.id),
        wake_action=WakeAction.FINALIZE,
        deadline_at=_utcnow() + timedelta(hours=1),
        reason=f"parked on compute job {job.id}",
    )
    await db_session.commit()

    job.status = ComputeJobStatus.SUCCEEDED
    job.outcome = ComputeJobOutcome.SUCCESS
    job.final_cost_usd = 0.22
    job.accrued_cost_usd = 0.22
    db_session.add(job)
    await fire_task_wakes(
        db_session,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(job.id),
    )
    await db_session.commit()

    # Simulate the crash window: call deliver_wake and commit (FIRED→DELIVERED),
    # but do NOT call run_compute_wake_post_commit afterward. Under the old code,
    # the marker would be absent here. With the fix, it must already be present.
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    advanced = await deliver_wake(db_session, reg, now=_utcnow())
    assert advanced is True
    await db_session.commit()

    # Snapshot the id before expire_all: reading task.id on an expired instance
    # lazy-loads (MissingGreenlet under real asyncpg expiry).
    task_id = task.id

    # Verify the marker is present on the DELIVERED row (seeded at delivery time).
    db_session.expire_all()
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert reg.delivery_state == WakeDeliveryState.DELIVERED
    assert _COMPUTE_WAKE_POST_COMMIT_META_KEY in (reg.source_metadata or {})

    # No knowledge document exists yet because run_compute_wake_post_commit was
    # never called (simulating the crash).
    docs = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert docs == []

    # Now let the sweep run. It should find the DELIVERED row with the marker and
    # call run_compute_wake_post_commit, persisting exactly one knowledge document.
    monkeypatch.setattr(
        "app.task_completion.broadcast_to_project",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.knowledge_completion._analyze_compute_experiment_with_llm",
        AsyncMock(return_value=(None, "precondition-not-met")),
    )

    again = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert again == 0  # No new FIRED rows delivered

    db_session.expire_all()
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    docs = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task_id
                )
            )
        )
        .scalars()
        .all()
    )
    # Marker cleared after successful side effects
    assert _COMPUTE_WAKE_POST_COMMIT_META_KEY not in (reg.source_metadata or {})
    # Exactly one knowledge document persisted, no duplicates
    assert len(docs) == 1
    assert docs[0].source_task_id == task_id

    # Third sweep: no marker, no new work — idempotent
    third = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert third == 0
    db_session.expire_all()
    docs_again = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == task_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(docs_again) == 1  # Still exactly one, no duplicates


@pytest.mark.asyncio
async def test_wake_post_commit_retry_skips_unmarked_delivered_rows(
    db_session, monkeypatch
):
    """Old DELIVERED rows without the retry marker must not starve the retry batch.

    Creates batch_size DELIVERED compute-wake rows WITHOUT the marker (simulating
    old already-processed rows), then creates ONE DELIVERED row WITH the marker.
    A single sweep_deliver_task_wakes call must find and process the marked row,
    clearing the marker and persisting a KnowledgeDocument, proving the SQL
    has_key filter excludes the markerless rows before LIMIT is applied.
    """
    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_completion import _COMPUTE_WAKE_POST_COMMIT_META_KEY
    from app.task_lifecycle import deliver_wake, fire_task_wakes, park_task
    from app.time_utils import _utcnow

    project = Project(name="Compute Wake Retry Starvation", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    # Snapshot the id: the db_session.expire_all() inside the filler loop below
    # expires `project`, and reading project.id on the next iteration would
    # lazy-load (MissingGreenlet under real asyncpg expiry).
    project_id = project.id

    # --- Build batch_size DELIVERED rows WITHOUT the marker ---
    # Use the default batch_size of sweep_deliver_task_wakes (10).
    batch_size = 10
    for _ in range(batch_size):
        filler_task = Task(
            project_id=project_id,
            title="compute_experiment: filler no-marker",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "compute_experiment", "base_branch": "main"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
        )
        db_session.add(filler_task)
        await db_session.commit()
        await db_session.refresh(filler_task)

        filler_job = await compute_jobs.create_compute_job(
            db_session,
            filler_task,
            command="echo filler",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )
        await park_task(
            db_session,
            filler_task,
            wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
            wake_source_ref=str(filler_job.id),
            wake_action=WakeAction.FINALIZE,
            deadline_at=_utcnow() + timedelta(hours=1),
            reason=f"parked on filler job {filler_job.id}",
        )
        await db_session.commit()

        filler_job.status = ComputeJobStatus.SUCCEEDED
        filler_job.outcome = ComputeJobOutcome.SUCCESS
        filler_job.final_cost_usd = 0.01
        filler_job.accrued_cost_usd = 0.01
        db_session.add(filler_job)
        await fire_task_wakes(
            db_session,
            wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
            wake_source_ref=str(filler_job.id),
        )
        await db_session.commit()

        # Deliver directly to DELIVERED state (marker is seeded by deliver_wake).
        filler_reg = (
            (
                await db_session.execute(
                    select(TaskWakeRegistration).where(
                        TaskWakeRegistration.wake_source_ref == str(filler_job.id)
                    )
                )
            )
            .scalars()
            .one()
        )
        await deliver_wake(db_session, filler_reg, now=_utcnow())
        await db_session.commit()

        # Now strip the marker to simulate an already-completed delivery with no
        # pending post-commit side effects.  Snapshot the id first: reading
        # filler_job.id after expire_all lazy-loads (MissingGreenlet on asyncpg).
        filler_job_id = str(filler_job.id)
        db_session.expire_all()
        filler_reg = (
            (
                await db_session.execute(
                    select(TaskWakeRegistration).where(
                        TaskWakeRegistration.wake_source_ref == filler_job_id
                    )
                )
            )
            .scalars()
            .one()
        )
        meta = dict(filler_reg.source_metadata or {})
        meta.pop(_COMPUTE_WAKE_POST_COMMIT_META_KEY, None)
        filler_reg.source_metadata = meta
        db_session.add(filler_reg)
        await db_session.commit()

    # --- Build ONE DELIVERED row WITH the marker ---
    marked_task = Task(
        project_id=project_id,
        title="compute_experiment: needs retry",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
    )
    db_session.add(marked_task)
    await db_session.commit()
    await db_session.refresh(marked_task)

    marked_job = await compute_jobs.create_compute_job(
        db_session,
        marked_task,
        command="echo ok",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    await park_task(
        db_session,
        marked_task,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(marked_job.id),
        wake_action=WakeAction.FINALIZE,
        deadline_at=_utcnow() + timedelta(hours=1),
        reason=f"parked on marked job {marked_job.id}",
    )
    await db_session.commit()

    marked_job.status = ComputeJobStatus.SUCCEEDED
    marked_job.outcome = ComputeJobOutcome.SUCCESS
    marked_job.final_cost_usd = 0.22
    marked_job.accrued_cost_usd = 0.22
    db_session.add(marked_job)
    await fire_task_wakes(
        db_session,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(marked_job.id),
    )
    await db_session.commit()

    # Snapshot the ids before the expire_all() calls below: reading
    # marked_job.id / marked_task.id on an expired instance lazy-loads
    # (MissingGreenlet under real asyncpg expiry).
    marked_job_id = str(marked_job.id)
    marked_task_id = marked_task.id

    # Deliver the marked row — marker is seeded atomically at delivery time.
    marked_reg = (
        (
            await db_session.execute(
                select(TaskWakeRegistration).where(
                    TaskWakeRegistration.wake_source_ref == marked_job_id
                )
            )
        )
        .scalars()
        .one()
    )
    await deliver_wake(db_session, marked_reg, now=_utcnow())
    await db_session.commit()

    # Verify marker is present before the sweep.
    db_session.expire_all()
    marked_reg = (
        (
            await db_session.execute(
                select(TaskWakeRegistration).where(
                    TaskWakeRegistration.wake_source_ref == marked_job_id
                )
            )
        )
        .scalars()
        .one()
    )
    assert _COMPUTE_WAKE_POST_COMMIT_META_KEY in (marked_reg.source_metadata or {})

    # Patch side effects so the sweep can complete.
    monkeypatch.setattr(
        "app.task_completion.broadcast_to_project",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.knowledge_completion._analyze_compute_experiment_with_llm",
        AsyncMock(return_value=(None, "precondition-not-met")),
    )

    # Run sweep with the default batch_size. The batch_size markerless rows must NOT
    # fill the window — the SQL has_key filter should exclude them and the one marked
    # row must be found and retried.
    await sweep_deliver_task_wakes(db_session, _utcnow(), batch_size=batch_size)

    db_session.expire_all()
    marked_reg = (
        (
            await db_session.execute(
                select(TaskWakeRegistration).where(
                    TaskWakeRegistration.wake_source_ref == marked_job_id
                )
            )
        )
        .scalars()
        .one()
    )
    docs = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == marked_task_id
                )
            )
        )
        .scalars()
        .all()
    )
    # Marker must be cleared — side effects ran successfully.
    assert _COMPUTE_WAKE_POST_COMMIT_META_KEY not in (marked_reg.source_metadata or {})
    # Exactly one knowledge document persisted for the marked task.
    assert len(docs) == 1
    assert docs[0].source_task_id == marked_task_id


@pytest.mark.asyncio
async def test_wake_post_commit_retry_backlog_cannot_starve_newer_markers(
    db_session, monkeypatch
):
    """A backlog of failing marked rows with future next_retry_after cannot starve newer ready rows.

    Creates batch_size DELIVERED rows WITH the marker AND with
    ``_COMPUTE_WAKE_NEXT_RETRY_KEY`` set to a far-future timestamp (simulating
    rows in their backoff window). Then creates ONE DELIVERED row WITH the marker
    but WITHOUT ``_COMPUTE_WAKE_NEXT_RETRY_KEY`` (ready to be retried now).

    A single sweep_deliver_task_wakes call must skip the backlogged rows (backoff
    window not expired) and process the ready row, clearing its marker and
    persisting exactly one KnowledgeDocument. The backlog rows must remain
    untouched (marker still present).
    """
    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_completion import (
        _COMPUTE_WAKE_NEXT_RETRY_KEY,
        _COMPUTE_WAKE_POST_COMMIT_META_KEY,
    )
    from app.task_lifecycle import deliver_wake, fire_task_wakes, park_task
    from app.time_utils import _utcnow

    project = Project(name="Compute Wake Retry Starvation Backoff", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    # Snapshot the id: the db_session.expire_all() inside the backlog loop below
    # expires `project`, and reading project.id on the next iteration would
    # lazy-load (MissingGreenlet under real asyncpg expiry).
    project_id = project.id

    batch_size = 10
    # --- Build batch_size DELIVERED rows WITH the marker AND a far-future backoff ---
    far_future = (_utcnow() + timedelta(hours=24)).isoformat()
    backlog_regs = []
    for _ in range(batch_size):
        backlog_task = Task(
            project_id=project_id,
            title="compute_experiment: backoff row",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "compute_experiment", "base_branch": "main"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
        )
        db_session.add(backlog_task)
        await db_session.commit()
        await db_session.refresh(backlog_task)

        backlog_job = await compute_jobs.create_compute_job(
            db_session,
            backlog_task,
            command="echo backlog",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )
        await park_task(
            db_session,
            backlog_task,
            wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
            wake_source_ref=str(backlog_job.id),
            wake_action=WakeAction.FINALIZE,
            deadline_at=_utcnow() + timedelta(hours=1),
            reason=f"parked on backlog job {backlog_job.id}",
        )
        await db_session.commit()

        backlog_job.status = ComputeJobStatus.SUCCEEDED
        backlog_job.outcome = ComputeJobOutcome.SUCCESS
        backlog_job.final_cost_usd = 0.01
        backlog_job.accrued_cost_usd = 0.01
        db_session.add(backlog_job)
        await fire_task_wakes(
            db_session,
            wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
            wake_source_ref=str(backlog_job.id),
        )
        await db_session.commit()

        # Deliver to DELIVERED state (marker seeded at delivery time).
        b_reg = (
            (
                await db_session.execute(
                    select(TaskWakeRegistration).where(
                        TaskWakeRegistration.wake_source_ref == str(backlog_job.id)
                    )
                )
            )
            .scalars()
            .one()
        )
        await deliver_wake(db_session, b_reg, now=_utcnow())
        await db_session.commit()

        # Inject a far-future backoff timestamp so this row is skipped by the
        # sweep.  Snapshot the id first: reading backlog_job.id after
        # expire_all lazy-loads (MissingGreenlet on asyncpg).
        backlog_job_id = str(backlog_job.id)
        db_session.expire_all()
        b_reg = (
            (
                await db_session.execute(
                    select(TaskWakeRegistration).where(
                        TaskWakeRegistration.wake_source_ref == backlog_job_id
                    )
                )
            )
            .scalars()
            .one()
        )
        meta = dict(b_reg.source_metadata or {})
        meta[_COMPUTE_WAKE_NEXT_RETRY_KEY] = far_future
        b_reg.source_metadata = meta
        db_session.add(b_reg)
        await db_session.commit()
        backlog_regs.append(b_reg.id)

    # --- Build ONE DELIVERED row WITH the marker and WITHOUT next_retry_after ---
    ready_task = Task(
        project_id=project_id,
        title="compute_experiment: ready row",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
    )
    db_session.add(ready_task)
    await db_session.commit()
    await db_session.refresh(ready_task)

    ready_job = await compute_jobs.create_compute_job(
        db_session,
        ready_task,
        command="echo ok",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    await park_task(
        db_session,
        ready_task,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(ready_job.id),
        wake_action=WakeAction.FINALIZE,
        deadline_at=_utcnow() + timedelta(hours=1),
        reason=f"parked on ready job {ready_job.id}",
    )
    await db_session.commit()

    ready_job.status = ComputeJobStatus.SUCCEEDED
    ready_job.outcome = ComputeJobOutcome.SUCCESS
    ready_job.final_cost_usd = 0.22
    ready_job.accrued_cost_usd = 0.22
    db_session.add(ready_job)
    await fire_task_wakes(
        db_session,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(ready_job.id),
    )
    await db_session.commit()

    # Snapshot the ids before the expire_all() calls below: reading
    # ready_job.id / ready_task.id on an expired instance lazy-loads
    # (MissingGreenlet under real asyncpg expiry).
    ready_job_id = str(ready_job.id)
    ready_task_id = ready_task.id

    ready_reg = (
        (
            await db_session.execute(
                select(TaskWakeRegistration).where(
                    TaskWakeRegistration.wake_source_ref == ready_job_id
                )
            )
        )
        .scalars()
        .one()
    )
    await deliver_wake(db_session, ready_reg, now=_utcnow())
    await db_session.commit()

    # Verify the ready row has the marker but no next_retry_after.
    db_session.expire_all()
    ready_reg = (
        (
            await db_session.execute(
                select(TaskWakeRegistration).where(
                    TaskWakeRegistration.wake_source_ref == ready_job_id
                )
            )
        )
        .scalars()
        .one()
    )
    assert _COMPUTE_WAKE_POST_COMMIT_META_KEY in (ready_reg.source_metadata or {})
    assert _COMPUTE_WAKE_NEXT_RETRY_KEY not in (ready_reg.source_metadata or {})

    # Patch side effects so the sweep can complete.
    monkeypatch.setattr(
        "app.task_completion.broadcast_to_project",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.knowledge_completion._analyze_compute_experiment_with_llm",
        AsyncMock(return_value=(None, "precondition-not-met")),
    )

    # Run sweep with batch_size. The batch_size backlog rows have a far-future
    # next_retry_after and must NOT fill the window — the SQL and Python guards
    # should skip them and the one ready row must be found and retried.
    await sweep_deliver_task_wakes(db_session, _utcnow(), batch_size=batch_size)

    db_session.expire_all()

    # The ready row must have its marker cleared (side effects ran successfully).
    ready_reg = (
        (
            await db_session.execute(
                select(TaskWakeRegistration).where(
                    TaskWakeRegistration.wake_source_ref == ready_job_id
                )
            )
        )
        .scalars()
        .one()
    )
    assert _COMPUTE_WAKE_POST_COMMIT_META_KEY not in (ready_reg.source_metadata or {})

    # Exactly one knowledge document persisted for the ready task.
    docs = (
        (
            await db_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.source_task_id == ready_task_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(docs) == 1
    assert docs[0].source_task_id == ready_task_id

    # All backlog rows must still have their marker (and far-future backoff) intact.
    for reg_id in backlog_regs:
        b_reg = (
            (
                await db_session.execute(
                    select(TaskWakeRegistration).where(
                        TaskWakeRegistration.id == reg_id
                    )
                )
            )
            .scalars()
            .one()
        )
        assert _COMPUTE_WAKE_POST_COMMIT_META_KEY in (b_reg.source_metadata or {}), (
            f"Backlog row {reg_id} had its marker cleared (starvation occurred)"
        )
        assert (b_reg.source_metadata or {}).get(
            _COMPUTE_WAKE_NEXT_RETRY_KEY
        ) == far_future, f"Backlog row {reg_id} had its backoff timestamp modified"


def test_build_compute_result_logs_shape_metrics_and_disposition():
    """The backend COMPUTE_RESULT builder mirrors the agent artifact contract.

    Covers the behavior that moved off the agent: honest outcome/cost fields, the
    intentional/killed_by disposition for a budget kill, command-reported metrics
    flattened to top-level numeric keys, and reserved-key collisions skipped.
    """
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        backend="gcp",
        machine_type="e2-standard-8",
        status=ComputeJobStatus.FAILED,
        outcome=ComputeJobOutcome.SPEND_CAP_EXCEEDED,
        terminal_reason="compute job terminated at spend cap",
        spend_cap_usd=2.5,
        accrued_cost_usd=2.5,
        final_cost_usd=2.5,
        polling_metadata={
            "result_metrics": {
                "accuracy": 0.91,
                "tokens_per_sec": "1234",
                "final_cost_usd": 999,  # collides with reserved key → skipped
                "ignored": "nope",  # non-numeric → dropped
            },
        },
    )

    result = json.loads(compute_jobs.build_compute_result_logs(job))

    assert result["job_id"] == str(job.id)
    assert result["outcome"] == "SPEND_CAP_EXCEEDED"
    assert result["final_cost_usd"] == pytest.approx(2.5)
    assert result["intentional_terminal"] is True
    assert result["killed_by"] == "budget"
    # Reported metrics flattened to top-level numeric keys.
    assert result["accuracy"] == pytest.approx(0.91)
    assert result["tokens_per_sec"] == pytest.approx(1234.0)
    # Reserved-key collision did not overwrite the job-level field.
    assert result["final_cost_usd"] == pytest.approx(2.5)
    # Non-numeric reported metric dropped.
    assert "ignored" not in result


def test_build_compute_result_logs_job_level_keys_stay_filtered():
    """The builder and the unmeasured-criterion diagnostic must agree on which
    keys are plumbing.

    ``build_compute_result_logs`` writes the ComputeJob's own fields into the
    same flat object the experiment's metrics land in.
    ``_COMPUTE_RESULT_NON_METRIC_KEYS`` in app/task_evaluators.py is the list of
    those fields, and nothing but a shared reader's diligence held the two
    together - so they drifted, the builder growing to fourteen job-level keys
    while the filter knew eight. The result was a diagnostic telling operators a
    command had "emitted" ``backend``, ``machine_type`` and ``result_reference``,
    and that their declared metric was therefore probably misspelled.

    Adding a job-level key to the builder without adding it there fails here.
    """
    from app.task_evaluators import (
        _COMPUTE_RESULT_NON_METRIC_KEYS,
        describe_emitted_compute_metric_keys,
    )

    # Stamped by _link_terminal_artifact and deliberately NOT filtered: they are
    # real numbers a success_criterion may name, evaluable even when the command
    # itself died. A new key here must be a conscious "this is evaluable" call.
    evaluable_backend_metrics = {"durable_file_count", "durable_payload_bytes"}

    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        backend="gcp",
        machine_type="a2-highgpu-1g",
        status=ComputeJobStatus.FAILED,
        outcome=ComputeJobOutcome.FAILURE,
        terminal_reason="command exited 1",
        result_reference="gs://bucket/run-1",
        spend_cap_usd=5.0,
        accrued_cost_usd=1.25,
        final_cost_usd=1.25,
        polling_metadata={
            "durable_file_count": 3,
            "durable_payload_bytes": 4096,
            # Plain prose on purpose: no balanced JSON object, so the stdout
            # metric fallback contributes nothing and every remaining key is
            # unambiguously the backend's own.
            "command_log": {
                "text": "ImportError: no module named torch",
                "truncated": False,
            },
        },
    )

    result = json.loads(compute_jobs.build_compute_result_logs(job))

    unfiltered = (
        set(result) - _COMPUTE_RESULT_NON_METRIC_KEYS - evaluable_backend_metrics
    )
    assert not unfiltered, (
        f"build_compute_result_logs writes job-level key(s) {sorted(unfiltered)} "
        "that _COMPUTE_RESULT_NON_METRIC_KEYS (app/task_evaluators.py) does not "
        "know about; the unmeasured-criterion diagnostic will report them as "
        "metrics the experiment command emitted. Add them there, or add them to "
        "evaluable_backend_metrics here if they are genuinely evaluable."
    )

    # The property that actually matters, asserted end to end: the only keys the
    # diagnostic attributes to this run are the two evaluable backend metrics,
    # and the job's reason for stopping is stated rather than listed as a metric.
    diag = describe_emitted_compute_metric_keys(json.dumps(result))
    assert diag.startswith(
        "emitted_keys=['durable_file_count', 'durable_payload_bytes']"
    ), diag
    assert "terminal_reason='command exited 1'" in diag


def test_build_compute_result_logs_embeds_command_log_text():
    """The builder embeds the bounded command-output log so downstream analysis can
    find results that live in stdout/stderr rather than in $BLUEPRINT_RESULT_FILE."""
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        backend="gcp",
        machine_type="g2-standard-4",
        status=ComputeJobStatus.SUCCEEDED,
        outcome=ComputeJobOutcome.SUCCESS,
        accrued_cost_usd=0.5,
        final_cost_usd=0.5,
        polling_metadata={
            "command_log": {
                "text": "act-1-clear improvement: +0.16\n",
                "truncated": True,
            },
        },
    )
    result = json.loads(compute_jobs.build_compute_result_logs(job))
    assert "act-1-clear improvement: +0.16" in result["command_log_text"]
    assert result["command_log_truncated"] is True


def test_build_compute_result_logs_caps_command_log_text():
    """A log over the embed cap keeps the HEAD and the TAIL (results live at the end),
    eliding the middle, so it cannot bloat the JSON yet a downstream verdict still sees
    the end-of-run summary."""
    # Distinct head and tail markers so we can prove BOTH ends survive the cap.
    text = "HEAD_MARKER" + ("x" * 20000) + "TAIL_MARKER"
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        backend="gcp",
        machine_type="g2-standard-4",
        status=ComputeJobStatus.SUCCEEDED,
        outcome=ComputeJobOutcome.SUCCESS,
        accrued_cost_usd=0.5,
        final_cost_usd=0.5,
        polling_metadata={"command_log": {"text": text, "truncated": False}},
    )
    result = json.loads(compute_jobs.build_compute_result_logs(job))
    embed = result["command_log_text"]
    # Bounded: head(4000) + marker + tail(12000) ≈ 16k, never the full 20k.
    assert len(embed) < len(text)
    assert len(embed) <= 16200
    assert embed.startswith("HEAD_MARKER")   # head retained
    assert embed.endswith("TAIL_MARKER")      # tail (end-of-run results) retained
    assert "elided" in embed                   # middle marked elided
    assert result["command_log_truncated"] is True


def test_build_compute_result_logs_omits_command_log_when_absent():
    """No command_log in polling_metadata ⇒ no command_log_text key (backward-compat)."""
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        backend="local",
        machine_type="e2-standard-2",
        status=ComputeJobStatus.SUCCEEDED,
        outcome=ComputeJobOutcome.SUCCESS,
        accrued_cost_usd=0.1,
        final_cost_usd=0.1,
    )
    result = json.loads(compute_jobs.build_compute_result_logs(job))
    assert "command_log_text" not in result
    assert "command_log_truncated" not in result


def test_build_compute_result_logs_stdout_metrics_fallback():
    """When NO result file metrics exist but the command printed a JSON object to
    stdout, the builder recovers its numeric keys as top-level metrics so a metric
    that only reached stdout is still measurable."""
    job = _make_job_with_command_log(
        'training run complete\n{"accuracy": 0.93, "loss": "0.12", "label": "run-a"}\n'
    )
    result = json.loads(compute_jobs.build_compute_result_logs(job))
    # Numeric keys recovered (numeric strings coerced); non-numeric dropped.
    assert result["accuracy"] == pytest.approx(0.93)
    assert result["loss"] == pytest.approx(0.12)
    assert "label" not in result
    # The bounded command-log text is still embedded alongside the metrics.
    assert "training run complete" in result["command_log_text"]


def test_build_compute_result_logs_result_file_wins_over_stdout():
    """An explicit $BLUEPRINT_RESULT_FILE write (result_metrics) ALWAYS wins over a
    conflicting value the command also printed to stdout."""
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        backend="gcp",
        machine_type="g2-standard-4",
        status=ComputeJobStatus.SUCCEEDED,
        outcome=ComputeJobOutcome.SUCCESS,
        accrued_cost_usd=0.5,
        final_cost_usd=0.5,
        polling_metadata={
            "result_metrics": {"accuracy": 0.99},
            "command_log": {"text": '{"accuracy": 0.10}\n', "truncated": False},
        },
    )
    result = json.loads(compute_jobs.build_compute_result_logs(job))
    assert result["accuracy"] == pytest.approx(0.99)


def test_build_compute_result_logs_stdout_fallback_last_object_wins():
    """When the command prints several JSON objects, the LAST balanced object wins —
    the final reported result, not an intermediate one."""
    job = _make_job_with_command_log(
        '{"accuracy": 0.50}\nepoch 1\n{"accuracy": 0.88}\nfinal\n'
    )
    result = json.loads(compute_jobs.build_compute_result_logs(job))
    assert result["accuracy"] == pytest.approx(0.88)


def test_build_compute_result_logs_stdout_fallback_recovers_ab_metric_series():
    """A single-command A/B experiment that prints the same metric once per arm
    (fp32 then fp16, both emitting ``median_matmul_ms``) must keep BOTH values.

    Regression guard modeled on the real V100 fp16-vs-fp32 matmul proof: the
    command printed ``{"median_matmul_ms": 81.6...}`` then
    ``{"median_matmul_ms": 12.5...}``. Last-object-wins keeps the fp16 value as
    the canonical ``median_matmul_ms``, but the fp32 arm must remain recoverable
    under ``median_matmul_ms__series`` so the comparison that IS the proof is not
    silently reduced to a single number.
    """
    job = _make_job_with_command_log(
        'pulling image...\n{"median_matmul_ms": 81.60714758560061}\n'
        '{"median_matmul_ms": 12.5942244194448}\n'
    )
    result = json.loads(compute_jobs.build_compute_result_logs(job))
    # Canonical value stays the last emission (last-object-wins unchanged).
    assert result["median_matmul_ms"] == pytest.approx(12.5942244194448)
    # Both arms recovered, in emission order, so fp32 (81.6) is not lost.
    assert result["median_matmul_ms__series"] == pytest.approx(
        [81.60714758560061, 12.5942244194448]
    )
    # And the A/B ratio (the actual hypothesis: fp16 >= 1.5x fp32) is now
    # computable from structured data alone.
    fp32, fp16 = result["median_matmul_ms__series"]
    assert fp32 / fp16 >= 1.5


def test_build_compute_result_logs_stdout_fallback_no_series_for_single_value():
    """A metric emitted once (or repeatedly with the SAME value) gets no
    ``__series`` companion — the recovery only fires for a real A/B spread."""
    job = _make_job_with_command_log(
        '{"accuracy": 0.9}\nepoch\n{"accuracy": 0.9}\n'
    )
    result = json.loads(compute_jobs.build_compute_result_logs(job))
    assert result["accuracy"] == pytest.approx(0.9)
    assert "accuracy__series" not in result


def test_build_compute_result_logs_stdout_fallback_skips_reserved_keys():
    """The stdout fallback can never overwrite a reserved job-level field or a
    backend-derived durable key."""
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        backend="gcp",
        machine_type="g2-standard-4",
        status=ComputeJobStatus.SUCCEEDED,
        outcome=ComputeJobOutcome.SUCCESS,
        accrued_cost_usd=0.5,
        final_cost_usd=0.5,
        polling_metadata={
            "durable_file_count": 3,
            "command_log": {
                "text": '{"final_cost_usd": 999, "durable_file_count": 0, "f1": 0.7}\n',
                "truncated": False,
            },
        },
    )
    result = json.loads(compute_jobs.build_compute_result_logs(job))
    # Reserved/backend-derived keys untouched by the stdout fallback.
    assert result["final_cost_usd"] == pytest.approx(0.5)
    assert result["durable_file_count"] == pytest.approx(3)
    # A genuine new metric from stdout is still surfaced.
    assert result["f1"] == pytest.approx(0.7)


def test_build_compute_result_logs_falls_back_to_accrued_cost():
    """When final_cost_usd is unset, the builder falls back to accrued cost."""
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        backend="local",
        machine_type="e2-standard-2",
        status=ComputeJobStatus.SUCCEEDED,
        outcome=ComputeJobOutcome.SUCCESS,
        accrued_cost_usd=0.42,
        final_cost_usd=None,
    )
    result = json.loads(compute_jobs.build_compute_result_logs(job))
    assert result["final_cost_usd"] == pytest.approx(0.42)
    assert result["outcome"] == "SUCCESS"
    assert result["intentional_terminal"] is True
    assert result["killed_by"] is None


def test_build_compute_result_logs_string_outcome_disposition():
    """Outcome stored as a raw string (not enum) still resolves intentional/killed_by.

    ComputeJob.outcome is a plain String column; SQLAlchemy may load it as a bare
    string rather than a ComputeJobOutcome enum instance. The disposition lookup must
    coerce the normalized string to the enum so the mapping is not silently missed.
    """
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        backend="gcp",
        machine_type="e2-standard-8",
        status=ComputeJobStatus.FAILED,
        outcome="SPEND_CAP_EXCEEDED",  # raw string, not ComputeJobOutcome enum
        terminal_reason="spend cap",
        accrued_cost_usd=1.5,
        final_cost_usd=1.5,
    )

    result = json.loads(compute_jobs.build_compute_result_logs(job))

    assert result["outcome"] == "SPEND_CAP_EXCEEDED"
    assert result["intentional_terminal"] is True
    assert result["killed_by"] == "budget"


def test_build_compute_result_logs_string_outcome_timed_out():
    """TIMED_OUT as a raw string also resolves correctly."""
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        backend="gcp",
        machine_type="e2-standard-8",
        status=ComputeJobStatus.FAILED,
        outcome="TIMED_OUT",  # raw string
        accrued_cost_usd=0.5,
        final_cost_usd=0.5,
    )

    result = json.loads(compute_jobs.build_compute_result_logs(job))

    assert result["outcome"] == "TIMED_OUT"
    assert result["intentional_terminal"] is True
    assert result["killed_by"] == "deadline"


# ── Release identity injection, echo, and echo parsing ──────────────────────


@pytest.mark.asyncio
async def test_build_launch_spec_injects_release_env_vars(db_session, monkeypatch):
    """_build_launch_spec injects BLUEPRINT_RELEASE_* when live_release_evidence is set."""
    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    task = await _make_compute_task(db_session)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python eval.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name="gcp",
    )
    evidence = {
        "promoted_sha": "abc123def456",
        "release_version_number": 9,
        "release_version_id": "11111111-0000-0000-0000-000000000001",
        "aggregate_serving_status": "serving_confirmed",
    }
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "live_release_evidence": evidence,
    }

    spec = compute_jobs._build_launch_spec(job)
    assert spec.env is not None
    assert spec.env["BLUEPRINT_RELEASE_SHA"] == "abc123def456"
    assert spec.env["BLUEPRINT_RELEASE_VERSION"] == "9"
    assert (
        spec.env["BLUEPRINT_RELEASE_VERSION_ID"]
        == "11111111-0000-0000-0000-000000000001"
    )


@pytest.mark.asyncio
async def test_build_launch_spec_no_env_injection_without_evidence(
    db_session, monkeypatch
):
    """_build_launch_spec does NOT inject BLUEPRINT_RELEASE_* for non-live-proof jobs."""
    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    task = await _make_compute_task(db_session)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python eval.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name="gcp",
    )
    spec = compute_jobs._build_launch_spec(job)
    env = spec.env or {}
    assert "BLUEPRINT_RELEASE_SHA" not in env
    assert "BLUEPRINT_RELEASE_VERSION" not in env
    assert "BLUEPRINT_RELEASE_VERSION_ID" not in env


@pytest.mark.asyncio
async def test_build_launch_spec_caller_env_takes_precedence_over_release_env(
    db_session, monkeypatch
):
    """Caller-provided env overrides injected release env vars."""
    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    task = await _make_compute_task(db_session)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python eval.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name="gcp",
    )
    evidence = {
        "promoted_sha": "abc123def456",
        "release_version_number": 9,
    }
    job.polling_metadata = {
        **(job.polling_metadata or {}),
        "live_release_evidence": evidence,
        "env": {"BLUEPRINT_RELEASE_SHA": "caller-override"},
    }
    spec = compute_jobs._build_launch_spec(job)
    assert spec.env["BLUEPRINT_RELEASE_SHA"] == "caller-override"


@pytest.mark.asyncio
async def test_build_launch_spec_includes_declared_metric(db_session, monkeypatch):
    """_build_launch_spec propagates declared_metric from polling_metadata to the spec."""
    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    task = await _make_compute_task(db_session)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python eval.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name="gcp",
        declared_metric="accuracy",
        no_progress_timeout_seconds=120,
    )

    spec = compute_jobs._build_launch_spec(job)
    assert spec.declared_metric == "accuracy", (
        "_build_launch_spec must carry declared_metric so backends "
        "can read it without re-accessing polling_metadata"
    )


@pytest.mark.asyncio
async def test_build_launch_spec_declared_metric_none_when_absent(db_session, monkeypatch):
    """_build_launch_spec returns declared_metric=None when no metric was declared."""
    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    task = await _make_compute_task(db_session)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python eval.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name="gcp",
    )

    spec = compute_jobs._build_launch_spec(job)
    assert spec.declared_metric is None
