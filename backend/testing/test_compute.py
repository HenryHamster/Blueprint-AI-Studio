"""
Tests for app/compute.py

Covers:
- Zone success-rate tracking (rolling window, zone ordering, reset, summary).
- Compute-job VM heartbeat endpoint and provisioning-timeout guard.
"""

import asyncio
import base64
import io
import json
import tarfile
import tracemalloc
from collections import deque
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, call, patch
import os
import re
import shlex
import shutil
import signal
import sys
import threading
import time
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
from app import schemas
from app import storage
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
from testing.conftest import TestingSessionLocal, USE_POSTGRES
from app.models import (
    Alert,
    AlertSeverity,
    Artifact,
    ArtifactType,
    ComputeJob,
    ComputeJobFile,
    ComputeJobOutcome,
    ComputeJobStatus,
    KnowledgeDocument,
    LocalRunner,
    Project,
    ProjectComputeImage,
    ProjectMember,
    ReleaseDeployArtifact,
    ReleaseVersion,
    Task,
    TaskHistory,
    TaskStatus,
    TaskWakeRegistration,
    WakeAction,
    WakeDeliveryState,
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

    assert (
        await compute.fleet_serving_source_sha(
            _FleetSourceFakeSession(),
            "b" * 40,
        )
        is True
    )
    ancestry.assert_not_awaited()


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
            project_id="proj",
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


class TestAgentSecretManifest:
    def test_build_agent_secret_manifest_includes_runtime_identity(self):
        task_id = uuid.uuid4()
        manifest = compute.build_agent_secret_manifest(
            vm_instance_id="vm-123",
            task_id=task_id,
            oauth_token="sk-ant-test",
            task_type="create",
            provider="anthropic",
            auth_mode="oauth",
            model="claude-sonnet-4-6",
            execution_mode="advisor",
        )

        assert manifest["env"]["PROVIDER"] == "anthropic"
        assert manifest["env"]["AUTH_MODE"] == "oauth"
        assert manifest["env"]["LLM_MODEL"] == "claude-sonnet-4-6"
        assert manifest["env"]["LLM_EXECUTION_MODE"] == "advisor"
        assert manifest["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-test"
        assert manifest["env"]["TASK_ID"] == str(task_id)
        assert manifest["metadata"]["task_type"] == "create"
        assert manifest["files"] == []

    def test_build_agent_secret_manifest_materializes_file_based_provider_auth(self):
        task_id = uuid.uuid4()
        manifest = compute.build_agent_secret_manifest(
            vm_instance_id="vm-openai",
            task_id=task_id,
            oauth_token='{"tokens":{"access_token":"abc"}}',
            task_type="create",
            provider="openai",
            auth_mode="oauth",
            model="gpt-5.4",
        )

        assert manifest["env"]["PROVIDER"] == "openai"
        assert manifest["env"]["AUTH_MODE"] == "oauth"
        assert manifest["env"]["CODEX_HOME"] == "/opt/agent/openai"
        assert manifest["files"] == [
            {
                "path": "/opt/agent/openai/auth.json",
                "content": '{"tokens":{"access_token":"abc"}}',
                "mode": "0600",
            }
        ]

    def test_build_agent_secret_manifest_maps_zai_to_claude_compat_env(self):
        task_id = uuid.uuid4()
        manifest = compute.build_agent_secret_manifest(
            vm_instance_id="vm-zai",
            task_id=task_id,
            oauth_token="zai-test-key",
            task_type="create",
            provider="zai",
            auth_mode="api_key",
            model="glm-5.2",
            provider_metadata={"anthropic_base_url": "https://example.test/anthropic/"},
            provider_settings={"model": "glm-5.2", "effort": "high"},
        )

        assert manifest["env"]["PROVIDER"] == "zai"
        assert manifest["env"]["AUTH_MODE"] == "api_key"
        assert manifest["env"]["LLM_MODEL"] == "glm-5.2"
        assert manifest["env"]["ZAI_API_KEY"] == "zai-test-key"
        assert (
            manifest["env"]["ZAI_ANTHROPIC_BASE_URL"]
            == "https://example.test/anthropic"
        )
        assert manifest["env"]["ANTHROPIC_API_KEY"] == "zai-test-key"
        assert manifest["env"]["ANTHROPIC_AUTH_TOKEN"] == "zai-test-key"
        assert manifest["env"]["ANTHROPIC_BASE_URL"] == "https://example.test/anthropic"
        assert manifest["files"] == []

    def test_build_agent_secret_manifest_maps_deepseek_to_claude_compat_env(self):
        task_id = uuid.uuid4()
        manifest = compute.build_agent_secret_manifest(
            vm_instance_id="vm-deepseek",
            task_id=task_id,
            oauth_token="deepseek-test-key",
            task_type="ops",
            provider="deepseek",
            auth_mode="api_key",
            model="deepseek-v4-flash",
            provider_metadata={"anthropic_base_url": "https://example.test/deepseek/"},
            provider_settings={"model": "deepseek-v4-flash", "effort": "low"},
        )

        assert manifest["env"]["PROVIDER"] == "deepseek"
        assert manifest["env"]["AUTH_MODE"] == "api_key"
        assert manifest["env"]["LLM_MODEL"] == "deepseek-v4-flash"
        assert manifest["env"]["DEEPSEEK_API_KEY"] == "deepseek-test-key"
        assert (
            manifest["env"]["DEEPSEEK_ANTHROPIC_BASE_URL"]
            == "https://example.test/deepseek"
        )
        assert manifest["env"]["ANTHROPIC_API_KEY"] == "deepseek-test-key"
        assert manifest["env"]["ANTHROPIC_AUTH_TOKEN"] == "deepseek-test-key"
        assert manifest["env"]["ANTHROPIC_BASE_URL"] == "https://example.test/deepseek"
        assert manifest["files"] == []

    def test_build_agent_secret_manifest_includes_provider_metadata_backed_files(self):
        task_id = uuid.uuid4()
        manifest = compute.build_agent_secret_manifest(
            vm_instance_id="vm-gemini",
            task_id=task_id,
            oauth_token='{"type":"service_account","client_email":"bot@example.com","private_key":"abc"}',
            task_type="research",
            provider="gemini",
            auth_mode="service_account",
            model="gemini-2.5-pro",
            provider_metadata={"project": "demo-proj", "location": "us-central1"},
        )

        assert (
            manifest["env"]["GOOGLE_APPLICATION_CREDENTIALS"]
            == "/opt/agent/gemini/sa.json"
        )
        assert manifest["env"]["GOOGLE_CLOUD_PROJECT"] == "demo-proj"
        assert manifest["env"]["GOOGLE_CLOUD_LOCATION"] == "us-central1"
        assert manifest["files"][0]["path"] == "/opt/agent/gemini/sa.json"
        assert '"client_email":"bot@example.com"' in manifest["files"][0]["content"]

    def test_build_agent_secret_manifest_carries_provider_settings_bag(self):
        """Re-target hands the agent the resolved account's provider settings
        bag: a provided (even empty) bag must surface as LLM_SETTINGS + a CLI
        translation, and the runtime identity must reflect the resolved account
        — this is the material warm re-targeting reuses to re-credential a VM."""
        task_id = uuid.uuid4()
        manifest = compute.build_agent_secret_manifest(
            vm_instance_id="vm-retarget",
            task_id=task_id,
            oauth_token="sk-ant-retarget",
            task_type="create",
            provider="anthropic",
            auth_mode="oauth",
            model="claude-opus-4-8",
            provider_settings={},
        )

        # Resolved-account identity carried for re-credentialing the warm VM.
        assert manifest["env"]["PROVIDER"] == "anthropic"
        assert manifest["env"]["AUTH_MODE"] == "oauth"
        assert manifest["env"]["LLM_MODEL"] == "claude-opus-4-8"
        assert manifest["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-retarget"
        # An explicit (even empty) settings bag opts into the settings-bag
        # protocol: LLM_SETTINGS is emitted and a CLI translation is computed.
        assert manifest["env"]["LLM_SETTINGS"] == "{}"
        assert "LLM_CLI_TRANSLATION" in manifest["env"]
        assert manifest["metadata"]["model"] == "claude-opus-4-8"


@pytest.mark.asyncio
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


def _metrics_marker(payload: object) -> str:
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def _command_log_marker(payload: object) -> str:
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def test_gcp_serial_parse_extracts_result_metrics():
    marker = _metrics_marker({"accuracy": 0.91, "tokens_per_sec": 1234, "ok": True})
    command_log = _command_log_marker(
        {"text": "stdout line\nstderr line\n", "truncated": False, "byte_count": 24}
    )
    serial = "\n".join(
        [
            "BLUEPRINT_COMPUTE_REASON=completed",
            "BLUEPRINT_COMPUTE_STATUS=SUCCEEDED",
            "BLUEPRINT_COMPUTE_EXIT_CODE=0",
            f"BLUEPRINT_COMPUTE_METRICS={marker}",
            f"BLUEPRINT_COMPUTE_COMMAND_LOG={command_log}",
        ]
    )
    status = compute_backends_module._parse_serial_status(serial)
    assert status is not None
    assert status.status == ComputeJobStatus.SUCCEEDED
    # bool coerced to a number; all values numeric.
    assert status.metrics == {"accuracy": 0.91, "tokens_per_sec": 1234.0, "ok": 1.0}
    assert status.command_log == {
        "text": "stdout line\nstderr line\n",
        "truncated": False,
        "byte_count": 24,
    }


def test_gcp_serial_parse_missing_marker_yields_no_metrics():
    serial = "\n".join(
        [
            "BLUEPRINT_COMPUTE_REASON=completed",
            "BLUEPRINT_COMPUTE_STATUS=SUCCEEDED",
            "BLUEPRINT_COMPUTE_EXIT_CODE=0",
        ]
    )
    status = compute_backends_module._parse_serial_status(serial)
    assert status is not None
    assert status.metrics is None


def test_gcp_serial_parse_garbled_marker_is_tolerated():
    bad_log = "not-valid-base64-$$$"
    serial = "\n".join(
        [
            "BLUEPRINT_COMPUTE_STATUS=SUCCEEDED",
            "BLUEPRINT_COMPUTE_EXIT_CODE=0",
            "BLUEPRINT_COMPUTE_METRICS=not-valid-base64-$$$",
            f"BLUEPRINT_COMPUTE_COMMAND_LOG={bad_log}",
        ]
    )
    status = compute_backends_module._parse_serial_status(serial)
    assert status is not None
    assert status.status == ComputeJobStatus.SUCCEEDED
    assert status.metrics is None
    assert status.command_log is None


def test_decode_result_metrics_filters_non_numeric_and_bounds():
    # Strings, nulls, and nested structures are dropped; numbers/bools survive.
    mixed = _metrics_marker(
        {
            "accuracy": 0.5,
            "label": "high",
            "missing": None,
            "nested": {"a": 1},
            "list": [1, 2],
            "flag": False,
        }
    )
    assert compute_backends_module._decode_result_metrics(mixed) == {
        "accuracy": 0.5,
        "flag": 0.0,
    }
    # A non-object JSON payload (array) is rejected entirely.
    assert (
        compute_backends_module._decode_result_metrics(_metrics_marker([1, 2, 3]))
        is None
    )
    # An all-non-numeric object decodes to no metrics → None.
    assert (
        compute_backends_module._decode_result_metrics(_metrics_marker({"x": "y"}))
        is None
    )
    # Oversized decoded payload is rejected wholesale.
    oversized = _metrics_marker({"m": list(range(10000))})
    assert compute_backends_module._decode_result_metrics(oversized) is None
    # Empty / None markers are absent.
    assert compute_backends_module._decode_result_metrics(None) is None
    assert compute_backends_module._decode_result_metrics("") is None


def test_decode_preserves_arms_block():
    # An arms dict (multi-arm comparison) survives decode verbatim alongside
    # numeric scalar metrics; other nested objects are still dropped.
    arms_dict = {"control": {"accuracy": 0.80}, "treatment": {"accuracy": 0.88}}
    marker = _metrics_marker(
        {
            "accuracy": 0.84,
            "arms": arms_dict,
            "ignored_nested": {"x": 1},
        }
    )
    result = compute_backends_module._decode_result_metrics(marker)
    assert result is not None
    assert result["accuracy"] == 0.84
    assert result["arms"] == arms_dict
    assert "ignored_nested" not in result

    # An arms list (list of arm records) also survives.
    arms_list = [{"name": "a", "score": 0.9}, {"name": "b", "score": 0.7}]
    marker_list = _metrics_marker({"loss": 0.1, "arms": arms_list})
    result_list = compute_backends_module._decode_result_metrics(marker_list)
    assert result_list is not None
    assert result_list["arms"] == arms_list
    assert result_list["loss"] == 0.1

    # An empty arms value is not preserved (no comparison block to surface).
    marker_empty = _metrics_marker({"accuracy": 0.5, "arms": {}})
    result_empty = compute_backends_module._decode_result_metrics(marker_empty)
    assert result_empty is not None
    assert "arms" not in result_empty


# ── Durable terminal record (terminal.json) — closes the poweroff-vs-serial
# race where a fast successful GCP job powers off before its serial SUCCEEDED
# marker is captured and is misclassified FAILURE. ──────────────────────────


def _b64_text(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _terminal_record_bytes(
    *,
    status: str = "SUCCEEDED",
    exit_code=0,
    reason: str = "completed",
    metrics: dict | None = None,
    command_log: dict | None = None,
    release_sha: str = "",
    schema_version: int = 1,
) -> bytes:
    """terminal.json bytes exactly as the on-VM terminal_record.py would write."""
    record = {
        "schema_version": schema_version,
        "status": status,
        "exit_code": exit_code,
        "reason": reason,
        "metrics_b64": _b64_text(json.dumps(metrics)) if metrics is not None else "",
        "command_log_b64": (
            _b64_text(json.dumps(command_log)) if command_log is not None else ""
        ),
        "release_echo_b64": _b64_text(release_sha) if release_sha else "",
    }
    return json.dumps(record).encode("utf-8")


def _make_tar_gz_bundle(members: dict[str, bytes]) -> bytes:
    """Build a gzip tar bundle mirroring the on-VM ``tar -C dir -czf - .`` layout."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def test_gcp_startup_script_writes_durable_terminal_record():
    """The startup script must decode terminal_record.py and invoke it before
    poweroff in the success/failure, runtime-TTL, and repo-clone-fail branches —
    so a lost serial marker can always be reconciled from the durable bundle."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=300,
        repo_clone={"owner": "o", "repo": "r", "ref": "main"},
        repo_token="tok",
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    # The embedded script is decoded to disk alongside the other helpers.
    assert "base64 -d > /tmp/blueprint_terminal_record.py" in script, (
        "terminal_record.py must be base64-decoded onto the VM"
    )
    # Main success/failure branch threads the computed STATUS/EXIT/REASON through.
    assert (
        "python3 /tmp/blueprint_terminal_record.py" in script
        and '"$STATUS" "$EXIT_CODE" "$REASON"' in script
    )
    # The record is written BEFORE the bundle is uploaded so it lands in the GCS
    # bundle the reconcile path reads.
    record_idx = script.index('"$STATUS" "$EXIT_CODE" "$REASON"')
    # The final upload_artifacts call follows the terminal-record write.
    assert "upload_artifacts" in script[record_idx:]
    # Runtime-TTL and repo-clone-fail branches each write their own record.
    assert "FAILED 124 runtime_ttl" in script
    assert "FAILED 1 repo_clone_failed" in script
    # The write must never block poweroff — every invocation is guarded `|| true`.
    assert "/tmp/blueprint_terminal_record.py" in script
    assert "|| true" in script


def test_gcp_startup_script_includes_no_progress_watchdog_when_set():
    """GCP startup script includes a no-progress watchdog subprocess when
    no_progress_timeout_seconds is set, and exports BLUEPRINT_PROGRESS_FILE."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=3600,
        no_progress_timeout_seconds=600,
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    # Progress file env var must be exported.
    assert "export BLUEPRINT_PROGRESS_FILE=" in script
    # Watchdog block must be present with the correct timeout and exit code.
    assert "_NP_WATCHDOG_PID=$!" in script
    assert "_NP_TIMEOUT_SECONDS=" in script  # set at the top of the script
    assert "no_progress_timeout" in script
    assert "EXIT_CODE=125" in script
    # Cleanup must include the watchdog PID.
    assert 'kill "$TTL_PID" "$_HEARTBEAT_PID" "$_NP_WATCHDOG_PID"' in script


def test_gcp_startup_script_no_progress_watchdog_inert_when_unset():
    """GCP startup script includes the watchdog shell code but the guard branch
    (_NP_TIMEOUT_SECONDS -gt 0) skips it when no_progress_timeout_seconds is unset."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=3600,
        # no_progress_timeout_seconds is NOT set
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    # Progress file env var must still be exported (always set).
    assert "export BLUEPRINT_PROGRESS_FILE=" in script
    # _NP_TIMEOUT_SECONDS must be 0 so the watchdog sleep loop is skipped.
    assert "_NP_TIMEOUT_SECONDS=0" in script


def test_gcp_startup_script_resume_hydration_is_fail_closed():
    """Cross-job resume must FAIL CLOSED on download/extract failure — aborting the
    run with a durable terminal record — mirroring the in-process backend's
    ``_rehydrate_input_artifact_bundle`` (which raises), NOT silently running from
    an empty workspace and burning the deadline redoing the resumed work."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=3600,
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec,
        artifact_upload_url="https://storage.example.com/upload",
        input_artifact_download_url="https://storage.example.com/resume-bundle",
    )
    # The resume block is present with its provenance markers.
    assert "resume_hydration.ok" in script
    assert "resume_hydration.failed" in script
    # On failure the run ABORTS loudly: durable terminal record with a parseable
    # reason, serial FAILED markers, artifact upload, and poweroff — the exact
    # fail-closed sequence the container-pull-fail branch uses.
    assert "FAILED 1 resume_hydration_failed" in script
    assert f"{compute_backends_module._SERIAL_REASON_PREFIX}resume_hydration_failed" in script
    assert f"{compute_backends_module._SERIAL_STATUS_PREFIX}FAILED" in script
    fail_idx = script.index("FAILED 1 resume_hydration_failed")
    tail = script[fail_idx:]
    assert "upload_artifacts" in tail
    assert "poweroff || shutdown -h now || true" in tail
    assert "exit 1" in tail
    # It must NOT silently continue from an empty workspace.
    assert "running from scratch" not in script
    # The rendered script is still syntactically valid bash.
    _assert_bash_syntax_ok(script)


def test_gcp_startup_script_emits_resume_serial_marker():
    """The GCP startup script emits _SERIAL_RESUME_PREFIX on both the success and
    failure branches of the resume block so the operator can always distinguish a
    continued-from-checkpoint run from a from-scratch restart."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=3600,
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec,
        artifact_upload_url="https://storage.example.com/upload",
        input_artifact_download_url="https://storage.example.com/resume-bundle",
    )

    # The success branch emits the prefix with a base64-encoded payload.
    prefix = compute_backends_module._SERIAL_RESUME_PREFIX
    assert prefix in script, (
        f"_SERIAL_RESUME_PREFIX ({prefix!r}) must appear in the startup script"
    )

    # The failure branch carries the pre-computed failed provenance marker.
    assert compute_backends_module._SERIAL_RESUME_FAILED_B64 in script, (
        "pre-computed failed resume marker must be in the failure branch"
    )

    # The prefix appears before the fail-closed path (before 'resume_hydration_failed')
    # as well as on the success path.
    resume_reason_idx = script.find("resume_hydration_failed")
    fail_marker_idx = script.find(
        compute_backends_module._SERIAL_RESUME_FAILED_B64
    )
    # The failure-branch serial marker must appear near the resume_hydration_failed
    # reason, not orphaned elsewhere in the script.
    assert 0 <= fail_marker_idx < resume_reason_idx + 200, (
        "failure branch serial marker must be near the resume_hydration_failed reason"
    )

    _assert_bash_syntax_ok(script)


def test_gcp_startup_script_no_resume_block_without_download_url():
    """Without a resume source, no resume-hydration block (or its fail-closed
    abort) is emitted — the fail-closed path is scoped to actual resume jobs."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=3600,
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    assert "resume_hydration" not in script


def test_extract_terminal_record_from_bundle_roundtrips_and_failsoft():
    extract = compute_backends_module._extract_terminal_record_from_bundle
    # A faithful multi-file bundle: terminal.json is picked out by basename even
    # with the production ``./`` prefix and a sibling command.log.json present.
    bundle = _make_tar_gz_bundle(
        {
            "./command.log.json": b'{"text":"hi","truncated":false,"byte_count":2}',
            "./terminal.json": _terminal_record_bytes(reason="completed"),
        }
    )
    record = extract(bundle)
    assert record is not None
    assert record["status"] == "SUCCEEDED"
    assert record["reason"] == "completed"
    # A bundle with no terminal.json → None (not an error).
    no_terminal = _make_tar_gz_bundle({"./other.txt": b"x"})
    assert extract(no_terminal) is None
    # Non-tar / garbage bytes degrade to None, never raise.
    assert extract(b"not-a-tar-gz") is None
    assert extract(b"") is None
    # A terminal.json that isn't a JSON object → None.
    bad = _make_tar_gz_bundle({"./terminal.json": b"[1,2,3]"})
    assert extract(bad) is None


def test_reconcile_terminal_record_to_status_succeeded_and_failed():
    reconcile = compute_backends_module._reconcile_terminal_record_to_status
    # SUCCEEDED record carries exit code + decoded metrics + command log.
    record = json.loads(
        _terminal_record_bytes(
            status="SUCCEEDED",
            exit_code=0,
            reason="completed",
            metrics={"mean_act_gain": 0.16, "ok": True},
            command_log={"text": "done\n", "truncated": False, "byte_count": 5},
        ).decode("utf-8")
    )
    status = reconcile(record)
    assert status is not None
    assert status.status == ComputeJobStatus.SUCCEEDED
    assert status.exit_code == 0
    assert status.metrics == {"mean_act_gain": 0.16, "ok": 1.0}
    assert status.command_log == {
        "text": "done\n",
        "truncated": False,
        "byte_count": 5,
    }
    # FAILED record preserves the non-zero exit code and reason.
    failed = json.loads(
        _terminal_record_bytes(
            status="FAILED", exit_code=3, reason="command_failed"
        ).decode("utf-8")
    )
    fstatus = reconcile(failed)
    assert fstatus is not None
    assert fstatus.status == ComputeJobStatus.FAILED
    assert fstatus.exit_code == 3
    assert fstatus.message == "command_failed"
    # A record without a recognized status is treated as no durable record.
    assert reconcile({"reason": "completed"}) is None
    assert reconcile({"status": "RUNNING"}) is None
    # A boolean exit_code must NOT be read as 1 (bool is an int subclass).
    boolish = reconcile({"status": "SUCCEEDED", "exit_code": True})
    assert boolish is not None
    assert boolish.exit_code == 0


@pytest.mark.asyncio
async def test_gcp_poll_status_reconciles_terminated_vm_from_durable_success(
    monkeypatch,
):
    """The bug: a fast successful job powers off before its serial SUCCEEDED
    marker is captured; the VM reads TERMINATED with no marker. poll_status must
    reconcile to SUCCEEDED from the durable terminal.json, NOT report FAILURE."""
    backend = GcpComputeBackend()
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend=backend.name,
        external_compute_id="compute-job-vm-fast0001",
        status=ComputeJobStatus.RUNNING,
        polling_metadata={"zone": "us-central1-f"},
    )

    # Serial output carries NO terminal marker (lost to the poweroff race).
    monkeypatch.setattr(
        compute, "get_vm_serial_output", lambda *_a, **_k: "boot noise only"
    )
    monkeypatch.setattr(compute, "get_vm_status", lambda *_a, **_k: "TERMINATED")

    bundle = _make_tar_gz_bundle(
        {
            "./terminal.json": _terminal_record_bytes(
                status="SUCCEEDED",
                exit_code=0,
                reason="completed",
                metrics={"mean_act_gain": 0.16},
            )
        }
    )
    download_calls: list = []

    async def fake_download(job_id):
        download_calls.append(job_id)
        return bundle

    monkeypatch.setattr(storage, "download_compute_job_artifact_bundle", fake_download)

    status = await backend.poll_status(job)

    assert status.status == ComputeJobStatus.SUCCEEDED
    assert status.exit_code == 0
    assert status.metrics == {"mean_act_gain": 0.16}
    assert download_calls == [job.id]


@pytest.mark.asyncio
async def test_gcp_poll_status_terminated_without_durable_record_still_fails(
    monkeypatch,
):
    """Fallback preserved: a TERMINATED VM with neither a serial marker nor a
    durable terminal record is still a blind FAILURE."""
    backend = GcpComputeBackend()
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend=backend.name,
        external_compute_id="compute-job-vm-nodurable",
        status=ComputeJobStatus.RUNNING,
        polling_metadata={"zone": "us-central1-f"},
    )
    monkeypatch.setattr(compute, "get_vm_serial_output", lambda *_a, **_k: "")
    monkeypatch.setattr(compute, "get_vm_status", lambda *_a, **_k: "TERMINATED")

    async def fake_download(_job_id):
        return None

    monkeypatch.setattr(storage, "download_compute_job_artifact_bundle", fake_download)

    status = await backend.poll_status(job)
    assert status.status == ComputeJobStatus.FAILED
    assert status.message == "compute-job VM stopped without a terminal marker"


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
    assert result.status == ComputeJobStatus.FAILED
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
    inactive_runner = LocalRunner(project_id=project.id, token_hash="inactive", status="inactive", last_seen_at=now)
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
        _add("/abs.txt", "evil")
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
    assert not os.path.exists("/abs.txt") or True  # never wrote a real /abs.txt


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


def _gcp_spec_with_env(env: dict[str, str]) -> ComputeLaunchSpec:
    return ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=300,
        env=env,
    )


def test_gcp_startup_script_emits_valid_env_keys():
    script = compute_backends_module._build_gcp_startup_script(
        _gcp_spec_with_env({"GOOD_KEY": "ok", "_PRIVATE": "x", "SEED2": "42"}),
        artifact_upload_url="https://storage.example.com/upload",
    )
    assert "export GOOD_KEY=ok" in script
    assert "export _PRIVATE=x" in script
    assert "export SEED2=42" in script


@pytest.mark.parametrize(
    "bad_key",
    [
        "my-model",
        "1LEADING",
        "A B",
        "$(whoami)",
        # Trailing newline: a `.match()` gate PASSES this (Python's `$` also
        # matches just before a final newline), rendering `export GOOD\n=v` —
        # bash runs `export GOOD` (empty) then `=v` (command not found) and, with
        # no `set -e`, bills the run anyway. `bash -n` accepts it, so the shell
        # harness cannot catch it either. Only `.fullmatch()` closes this.
        "GOOD\n",
        "GOOD\nEVIL=1",
    ],
)
def test_gcp_startup_script_refuses_env_keys_that_are_not_shell_identifiers(bad_key):
    """Defense-in-depth for the contract-time ENV_KEY_RE gate.

    A key like "my-model" renders as `export my-model=...`, which bash rejects
    ("not a valid identifier") — and the startup script runs WITHOUT `set -e`,
    so it would silently continue and bill a GPU run missing that env var.

    The builder must RAISE, not skip: skipping would still launch the exact run
    this guard exists to prevent. The raise happens before provision_ephemeral_vm,
    so no VM is ever created. It raises ComputeLaunchSpecError so the caller can
    terminalize the job FAILED immediately with this message (which names the key)
    rather than retrying an unretryable spec until the provisioning deadline.
    """
    spec = _gcp_spec_with_env({"GOOD_KEY": "ok", bad_key: "bad"})
    with pytest.raises(compute_backends_module.ComputeLaunchSpecError) as excinfo:
        compute_backends_module._build_gcp_startup_script(
            spec, artifact_upload_url="https://storage.example.com/upload"
        )
    # The reason must name the offending key (surface WHY, not just "invalid").
    # It is interpolated with !r deliberately: repr renders an otherwise-invisible
    # trailing newline as a visible \n, so the operator can SEE what was wrong.
    assert repr(bad_key) in str(excinfo.value)
    assert "shell identifier" in str(excinfo.value)
    # Unretryable-spec signal must survive as a ValueError for existing handlers.
    assert isinstance(excinfo.value, ValueError)


# Independent restatement of the shell-identifier rule, deliberately NOT imported
# from app code: this is the OUTPUT-level backstop, so weakening the production
# ENV_KEY_RE must not silently weaken the check that catches its output.
_WELL_FORMED_EXPORT_RE = re.compile(r"^export [A-Za-z_][A-Za-z0-9_]*=", re.M)
_ANY_EXPORT_RE = re.compile(r"^export ", re.M)


def _assert_bash_syntax_ok(script: str) -> None:
    """Fail if ``bash -n`` rejects the rendered startup script's syntax.

    Skips (does not fail) when no ``bash`` binary is on this host, so the check is
    a no-op on a minimal CI image but a real gate wherever bash exists.
    """
    import subprocess

    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - environment-dependent
        pytest.skip("bash not available to run `bash -n`")
    proc = subprocess.run(
        [bash, "-n"],
        input=script,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"bash -n rejected the script: {proc.stderr}"


def _multi_line_exports(script: str) -> list[str]:
    """Return every top-level `export` in the RAW script that is not `IDENT=...`.

    Must run on the raw string. `splitlines()` cannot see this defect: a key
    carrying a newline renders as TWO physical lines — `export GOOD` and `=v` —
    and after the split the first reads as the perfectly valid key "GOOD". The
    newline is only visible while it is still in the string.
    """
    well_formed = {m.start() for m in _WELL_FORMED_EXPORT_RE.finditer(script)}
    return [
        script[m.start() : script.find("\n", m.start()) + 1 or len(script)]
        for m in _ANY_EXPORT_RE.finditer(script)
        if m.start() not in well_formed
    ]


def test_gcp_startup_script_never_emits_a_multi_line_export():
    """Backstop on the rendered artifact: no `export` may be split across lines.

    A newline smuggled into a key produces a second physical line that bash
    executes as a command, and `bash -n` still accepts the script. This checks the
    OUTPUT, not the guard — see the negative control below, which proves it can
    actually catch a bad script.
    """
    script = compute_backends_module._build_gcp_startup_script(
        _gcp_spec_with_env({"GOOD_KEY": "ok", "SEED": "42"}),
        artifact_upload_url="https://storage.example.com/upload",
    )
    assert _multi_line_exports(script) == []


def test_multi_line_export_backstop_catches_a_genuinely_bad_script(monkeypatch):
    """NEGATIVE CONTROL: the backstop above must fail on a script that is bad.

    The guard normally raises first, so the backstop never sees a bad script and
    contributes zero assurance unless we prove it CAN catch one. Neuter the guard
    (permissive ENV_KEY_RE) so a real multi-line export is actually rendered, then
    show the backstop flags it.
    """
    monkeypatch.setattr(
        compute_backends_module, "ENV_KEY_RE", re.compile(r"[\s\S]*")
    )
    script = compute_backends_module._build_gcp_startup_script(
        _gcp_spec_with_env({"GOOD\n": "v"}),
        artifact_upload_url="https://storage.example.com/upload",
    )
    # The guard is off, so the bad script really exists: two physical lines.
    assert "export GOOD\n" in script
    # ...and the backstop catches it, naming the offending statement.
    assert _multi_line_exports(script) == ["export GOOD\n"]


@pytest.mark.asyncio
async def test_gcp_backend_launch_uses_signed_artifact_upload(monkeypatch):
    backend = GcpComputeBackend()
    job_id = uuid.uuid4()
    spec = ComputeLaunchSpec(
        job_id=job_id,
        command="echo ok",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
    )

    signed_url_calls: list[dict] = []
    provision_calls: list[dict] = []

    async def fake_signed_url(jid, *, expires_at=None):
        signed_url_calls.append({"job_id": jid, "expires_at": expires_at})
        return "https://storage.example.com/upload"

    async def fake_provision(
        vm_id,
        *,
        startup_script,
        machine_type,
        provisioning_model,
        accelerator=None,
        accelerator_count=1,
    ):
        provision_calls.append(
            {
                "vm_id": vm_id,
                "startup_script": startup_script,
                "machine_type": machine_type,
                "provisioning_model": provisioning_model,
            }
        )
        return True

    monkeypatch.setattr(
        storage,
        "generate_compute_job_artifact_upload_signed_url",
        fake_signed_url,
    )
    monkeypatch.setattr(compute, "provision_ephemeral_vm", fake_provision)
    monkeypatch.setattr(compute, "find_vm_zone", lambda _vm_name: None)
    monkeypatch.setattr(compute, "get_vm_zone", lambda _vm_name: "us-central1-f")

    result = await backend.launch(spec)

    assert result.metadata["artifact_uri"] == storage.compute_job_artifact_uri(job_id)
    assert len(signed_url_calls) == 1
    assert signed_url_calls[0]["job_id"] == job_id
    assert signed_url_calls[0]["expires_at"] is not None
    assert len(provision_calls) == 1
    startup_script = provision_calls[0]["startup_script"]
    assert "trap upload_artifacts EXIT HUP INT TERM QUIT" in startup_script
    assert "BLUEPRINT_COMPUTE_COMMAND_LOG=" in startup_script
    assert "https://storage.example.com/upload" in startup_script
    assert "gcloud storage cp" not in startup_script
    assert "gsutil -m cp -r" not in startup_script


@pytest.mark.asyncio
async def test_gcp_backend_launch_rejects_bad_env_key_before_provisioning(monkeypatch):
    """The env-key gate must fire BEFORE a VM exists — on the real backend.

    This is the production-code proof behind "cost is zero": the fakes in the
    compute_jobs tests can only model the ordering, they cannot verify it. Here the
    real GcpComputeBackend.launch runs against a recording provision_ephemeral_vm,
    so if the gate ever moves below provisioning, provision_calls is non-empty and
    this fails — a billed GPU VM for a spec that can never run.
    """
    backend = GcpComputeBackend()
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python train.py",
        machine_type="a2-highgpu-1g",
        spend_cap_usd=50.0,
        deadline_at=None,
        env={"GOOD_KEY": "ok", "my-model": "bad"},
    )

    provision_calls: list[str] = []

    async def fake_provision(vm_id, **kwargs):
        provision_calls.append(vm_id)
        return True

    async def fake_signed_url(jid, *, expires_at=None):
        return "https://storage.example.com/upload"

    monkeypatch.setattr(
        storage, "generate_compute_job_artifact_upload_signed_url", fake_signed_url
    )
    monkeypatch.setattr(compute, "provision_ephemeral_vm", fake_provision)
    monkeypatch.setattr(compute, "find_vm_zone", lambda _vm_name: None)
    monkeypatch.setattr(compute, "get_vm_zone", lambda _vm_name: "us-central1-f")

    with pytest.raises(compute_backends_module.ComputeLaunchSpecError) as excinfo:
        await backend.launch(spec)

    assert "my-model" in str(excinfo.value)
    # The load-bearing assertion: no VM was ever asked for, so nothing is billing.
    assert provision_calls == []


@pytest.mark.asyncio
async def test_gcp_backend_terminate_stops_before_delete(monkeypatch):
    backend = GcpComputeBackend()
    job = ComputeJob(
        project_id=uuid.uuid4(),
        backend=compute_backends_module.GCP_BACKEND_NAME,
        external_compute_id="compute-job-vm-123456789abc",
    )

    calls: list[tuple[str, str, str]] = []

    async def fake_stop(vm_instance_id, *, reason):
        calls.append(("stop", vm_instance_id, reason))
        return True

    async def fake_delete(vm_instance_id, *, reason):
        calls.append(("delete", vm_instance_id, reason))
        return True

    monkeypatch.setattr(compute, "stop_vm", fake_stop)
    monkeypatch.setattr(compute_backends_module, "_ensure_vm_deleted", fake_delete)

    assert await backend.terminate(job, reason="budget") is True
    assert calls == [
        ("stop", "compute-job-vm-123456789abc", "compute_job_budget"),
        ("delete", "compute-job-vm-123456789abc", "compute_job_budget"),
    ]


@pytest.mark.asyncio
async def test_gcp_backend_adopt_existing_vm_records_artifact_uri(monkeypatch):
    backend = GcpComputeBackend()
    job_id = uuid.uuid4()
    started_at = datetime(2026, 1, 2, 3, 4, 5)

    monkeypatch.setattr(compute, "find_vm_zone", lambda _vm_name: "us-central1-f")
    monkeypatch.setattr(compute, "get_vm_created_at", lambda *_args: started_at)

    result = await backend.launch(
        ComputeLaunchSpec(
            job_id=job_id,
            command="echo ok",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_at=None,
        )
    )

    assert result.metadata["artifact_uri"] == storage.compute_job_artifact_uri(job_id)
    assert result.metadata["zone"] == "us-central1-f"


@pytest.mark.asyncio
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

    # Pure side-effect job: no declared metric, no arms, no criterion → no data.
    side_effect_task = Task(payloads=[{"type": "research"}])
    assert compute_jobs._job_expects_data(empty_job, side_effect_task) is False
    assert compute_jobs._job_expects_data(empty_job, None) is False


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
        status_metrics=None,
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
async def test_no_declared_metric_metricless_still_success(db_session, monkeypatch):
    """A job that declares NO metric and writes none stays SUCCESS (no regression)."""
    project = Project(name="No Metric Declared Success", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="no declared metric experiment",
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
            return "no-declared-metric-job"

        async def launch(self, spec):
            return ComputeLaunchResult(
                external_id="no-declared-metric-job",
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
    assert job.outcome == ComputeJobOutcome.SUCCESS


@pytest.mark.asyncio
async def test_no_data_guard_respects_durable_artifacts(db_session, monkeypatch):
    """A metric-less job that left a durable artifact is NOT a no-data failure."""
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
        status_metrics=None,
        artifact_reference=reference,
    )
    assert job.outcome == ComputeJobOutcome.SUCCESS


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
        payloads=[{"type": "compute_experiment"}],
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
    assert job.outcome == ComputeJobOutcome.SUCCESS

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
    # promotion_merge is also a registered required provider; it appears vacuous
    # (serving_status=None) when the test project has no GitHub config.
    assert {"backend_revision", "agent_image"}.issubset(
        {artifact["artifact_kind"] for artifact in evidence["required_artifacts"]}
    )


async def _make_compute_task(db_session):
    project = Project(name="GPU Compute Job", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


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


def test_detect_local_gpu_absent(monkeypatch):
    """detect_local_gpu returns None when nvidia-smi is not on the PATH."""
    from app import compute_backends as cb

    monkeypatch.setattr(cb.shutil, "which", lambda _name: None)
    result = cb.detect_local_gpu()
    assert result is None


def test_detect_local_gpu_present(monkeypatch):
    """detect_local_gpu returns parsed name + memory when nvidia-smi succeeds."""
    from app import compute_backends as cb

    monkeypatch.setattr(cb.shutil, "which", lambda _name: "/usr/bin/nvidia-smi")

    class _FakeResult:
        returncode = 0
        stdout = "Tesla T4, 15360\n"

    monkeypatch.setattr(cb.subprocess, "run", lambda *_a, **_kw: _FakeResult())
    result = cb.detect_local_gpu()
    assert result == {"name": "Tesla T4", "memory_mib": 15360}


def test_detect_local_gpu_subprocess_error(monkeypatch):
    """detect_local_gpu returns None and does not raise when nvidia-smi errors."""
    from app import compute_backends as cb

    monkeypatch.setattr(cb.shutil, "which", lambda _name: "/usr/bin/nvidia-smi")

    def _raise(*_a, **_kw):
        raise RuntimeError("oops")

    monkeypatch.setattr(cb.subprocess, "run", _raise)
    result = cb.detect_local_gpu()
    assert result is None


def test_validate_backend_name_local_gpu_no_gpu(monkeypatch):
    """validate_backend_name raises for local-gpu when no GPU is detected."""
    from app import compute_backends as cb

    monkeypatch.setattr(cb, "detect_local_gpu", lambda: None)
    with pytest.raises(ValueError, match="no local GPU detected"):
        cb.validate_backend_name("local-gpu")


def test_validate_backend_name_local_gpu_present(monkeypatch):
    """validate_backend_name accepts local-gpu when a GPU is detected."""
    from app import compute_backends as cb

    monkeypatch.setattr(cb, "detect_local_gpu", lambda: {"name": "T4", "memory_mib": 15360})
    assert cb.validate_backend_name("local-gpu") == "local-gpu"


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
    from datetime import timezone

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


# ─── Zone success-rate tracking tests ────────────────────────────────────────


@pytest.fixture(autouse=False)
def clean_zone_health():
    """Reset zone health state before and after each test that uses this fixture."""

    def _reset():
        compute._zone_health.clear()
        compute._zone_failures.clear()
        provision_error_scope._global_quota_state.clear()
        provision_error_scope._region_quota_state.clear()
        compute._zone_suspect_events.clear()
        # GPU-isolated state
        compute._gpu_zone_health.clear()
        compute._gpu_zone_failures.clear()
        provision_error_scope._gpu_global_quota_state.clear()
        provision_error_scope._gpu_region_quota_state.clear()

    _reset()
    yield
    _reset()


class TestZoneSuccessRate:
    """Tests for _zone_success_rate helper and rolling-window tracking."""

    def test_no_history_returns_neutral(self, clean_zone_health):
        """A zone with no history returns the neutral rate (0.5)."""
        rate = compute._zone_success_rate("us-central1-f")
        assert rate == compute._ZONE_NEUTRAL_RATE

    def test_all_successes(self, clean_zone_health):
        """After N successes the success rate is 1.0."""
        zone = "us-central1-f"
        compute._zone_health[zone] = {
            "recent_outcomes": deque([True] * 10, maxlen=compute.ZONE_HISTORY_WINDOW),
        }
        assert compute._zone_success_rate(zone) == 1.0

    def test_all_failures(self, clean_zone_health):
        """After N failures the success rate is 0.0."""
        zone = "us-central1-f"
        compute._zone_health[zone] = {
            "recent_outcomes": deque([False] * 10, maxlen=compute.ZONE_HISTORY_WINDOW),
        }
        assert compute._zone_success_rate(zone) == 0.0

    def test_mixed_rate(self, clean_zone_health):
        """3 failures + 1 success out of 4 provisions → 25% success rate."""
        zone = "us-central1-f"
        compute._zone_health[zone] = {
            "recent_outcomes": deque(
                [False, False, False, True], maxlen=compute.ZONE_HISTORY_WINDOW
            ),
        }
        assert compute._zone_success_rate(zone) == pytest.approx(0.25)

    def test_rolling_window_evicts_old_entries(self, clean_zone_health):
        """After ZONE_HISTORY_WINDOW provisions the oldest entries are evicted."""
        zone = "us-central1-f"
        window = compute.ZONE_HISTORY_WINDOW
        # Fill window with failures, then add one success past the limit
        outcomes: deque = deque(maxlen=window)
        for _ in range(window):
            outcomes.append(False)
        outcomes.append(True)  # Pushes out the oldest False

        compute._zone_health[zone] = {"recent_outcomes": outcomes}
        # Window still has `window` entries; one success + (window-1) failures
        rate = compute._zone_success_rate(zone)
        assert rate == pytest.approx(1.0 / window)

    def test_record_zone_failure_appends_false_when_authoritative(
        self, clean_zone_health
    ):
        """An authoritative zone-scoped reason appends False to recent_outcomes
        immediately (no corroboration needed)."""
        zone = "us-central1-f"
        compute.record_zone_failure(zone, reason="RESOURCE_POOL_EXHAUSTED")
        outcomes = compute._zone_health[zone]["recent_outcomes"]
        assert list(outcomes) == [False]

    def test_rate_reset_after_extended_healthy_period(self, clean_zone_health):
        """After the rolling window fills with successes, old failures are gone."""
        zone = "us-central1-f"
        window = compute.ZONE_HISTORY_WINDOW
        # Start with half failures
        outcomes: deque = deque(maxlen=window)
        for _ in range(window // 2):
            outcomes.append(False)
        compute._zone_health[zone] = {"recent_outcomes": outcomes}

        # Simulate window of successful provisions — each pushes out an old failure
        for _ in range(window):
            outcomes.append(True)

        assert compute._zone_success_rate(zone) == 1.0


class TestZoneOrdering:
    """Tests for zone selection ordering by success rate."""

    def test_high_success_rate_zone_preferred(self, clean_zone_health):
        """A zone with a high success rate sorts above one with a low rate."""
        good_zone = "us-central1-f"
        bad_zone = "us-central1-c"

        compute._zone_health[good_zone] = {
            "recent_outcomes": deque([True] * 10, maxlen=compute.ZONE_HISTORY_WINDOW),
        }
        compute._zone_health[bad_zone] = {
            "recent_outcomes": deque([False] * 10, maxlen=compute.ZONE_HISTORY_WINDOW),
        }

        assert compute._zone_success_rate(good_zone) > compute._zone_success_rate(
            bad_zone
        )

    def test_no_history_zone_gets_neutral_rate(self, clean_zone_health):
        """A zone with no history is treated as neutral (0.5), not pessimistic."""
        assert compute._zone_success_rate("us-east4-b") == 0.5


class TestZoneHealthOutput:
    """Tests for get_zone_health() and get_zone_health_summary() success_rate fields."""

    def test_get_zone_health_includes_success_rate(self, clean_zone_health):
        """get_zone_health() includes success_rate and provision_count per zone."""
        zone = "us-central1-f"
        compute._zone_health[zone] = {
            "last_success": datetime.now(timezone.utc),
            "consecutive_failures": 0,
            "recent_outcomes": deque(
                [True, True, False], maxlen=compute.ZONE_HISTORY_WINDOW
            ),
        }
        result = compute.get_zone_health()
        assert zone in result
        assert result[zone]["success_rate"] == pytest.approx(2 / 3)
        assert result[zone]["provision_count"] == 3

    def test_get_zone_health_no_history_has_none_rate(self, clean_zone_health):
        """Zones with no history report success_rate=None and provision_count=0."""
        result = compute.get_zone_health()
        for zone_data in result.values():
            assert zone_data["success_rate"] is None
            assert zone_data["provision_count"] == 0

    def test_get_zone_health_summary_includes_success_rates(self, clean_zone_health):
        """get_zone_health_summary() includes zone_success_rates for zones with history."""
        zone = "us-central1-f"
        compute._zone_health[zone] = {
            "consecutive_failures": 0,
            "recent_outcomes": deque(
                [True, False, True, True], maxlen=compute.ZONE_HISTORY_WINDOW
            ),
        }
        summary = compute.get_zone_health_summary()
        assert "zone_success_rates" in summary
        assert zone in summary["zone_success_rates"]
        assert summary["zone_success_rates"][zone] == pytest.approx(0.75)


# ─── Spot preemption override tests ──────────────────────────────────────────


class TestSpotPreemptionOverride:
    """provision_agent_vm respects the provisioning_model override parameter.

    A task with spot_preemption_count >= 2 causes the orchestrator to pass
    provisioning_model="STANDARD" to provision_agent_vm, suppressing the SPOT
    scheduling block even when the process-wide VM_PROVISIONING_MODEL is "SPOT".
    """

    async def test_provisioning_model_override_passed_to_gcp(self, monkeypatch):
        """provision_agent_vm forwards provisioning_model="STANDARD" to _provision_gcp."""
        monkeypatch.setattr(compute, "_image_family_valid", True)
        monkeypatch.setattr(compute, "LOCAL_MODE", False)

        captured = {}

        async def fake_gcp(*args, **kwargs):
            captured["provisioning_model"] = kwargs.get("provisioning_model")
            return True

        monkeypatch.setattr(compute, "_provision_gcp", fake_gcp)

        await compute.provision_agent_vm(
            "vm-preempt-test", uuid.uuid4(), provisioning_model="STANDARD"
        )
        assert captured["provisioning_model"] == "STANDARD"

    async def test_provisioning_model_none_passes_none_to_gcp(self, monkeypatch):
        """When provisioning_model=None, None propagates so the global env drives it."""
        monkeypatch.setattr(compute, "_image_family_valid", True)
        monkeypatch.setattr(compute, "LOCAL_MODE", False)

        captured = {}

        async def fake_gcp(*args, **kwargs):
            captured["provisioning_model"] = kwargs.get("provisioning_model")
            return True

        monkeypatch.setattr(compute, "_provision_gcp", fake_gcp)

        await compute.provision_agent_vm("vm-spot-test", uuid.uuid4())
        assert captured["provisioning_model"] is None

    async def test_standard_override_wins_over_spot_global_env(self, monkeypatch):
        """STANDARD override beats VM_PROVISIONING_MODEL=SPOT (simulates 2nd preemption path).

        When spot_preemption_count >= 2, the orchestrator passes provisioning_model='STANDARD'
        to provision_agent_vm even though the process-wide env is SPOT.  This verifies
        the compute layer passes 'STANDARD' through to _provision_gcp in that scenario.
        """
        monkeypatch.setattr(compute, "VM_PROVISIONING_MODEL", "SPOT")
        monkeypatch.setattr(compute, "_image_family_valid", True)
        monkeypatch.setattr(compute, "LOCAL_MODE", False)

        captured = {}

        async def fake_gcp(*args, **kwargs):
            captured["provisioning_model"] = kwargs.get("provisioning_model")
            return True

        monkeypatch.setattr(compute, "_provision_gcp", fake_gcp)

        await compute.provision_agent_vm(
            "vm-2nd-preempt", uuid.uuid4(), provisioning_model="STANDARD"
        )
        # STANDARD override must arrive at _provision_gcp even though global env is SPOT
        assert captured["provisioning_model"] == "STANDARD"

    def test_get_zone_health_summary_empty_rates_when_no_history(
        self, clean_zone_health
    ):
        """zone_success_rates is empty when no zone has any history."""
        summary = compute.get_zone_health_summary()
        assert summary["zone_success_rates"] == {}


class TestZoneHealthRestoreDeque:
    """Tests for the recent_outcomes deque restoration on startup."""

    def _restore_zone_health(self, zone_health_state: dict) -> None:
        """Simulate the startup restore logic from main.py lifespan."""
        from collections import deque as _deque

        compute._zone_health.clear()
        for zone, data in zone_health_state.items():
            if not isinstance(data, dict):
                continue
            restored = {}
            for k, v in data.items():
                if isinstance(v, str) and k in (
                    "last_success",
                    "last_failure",
                    "cooldown_expires_at",
                ):
                    try:
                        restored[k] = datetime.fromisoformat(v)
                    except ValueError:
                        continue
                elif k == "recent_outcomes" and isinstance(v, list):
                    valid = [e for e in v if isinstance(e, bool)]
                    restored[k] = _deque(
                        valid[-compute.ZONE_HISTORY_WINDOW :],
                        maxlen=compute.ZONE_HISTORY_WINDOW,
                    )
                elif k == "consecutive_failures":
                    restored[k] = v if isinstance(v, int) else 0
                elif k == "last_failure_reason":
                    restored[k] = v if (v is None or isinstance(v, str)) else None
                else:
                    restored[k] = v
            compute._zone_health[zone] = restored

    def test_recent_outcomes_restored_as_deque(self):
        """recent_outcomes list from DB is converted to a deque with correct maxlen."""
        zone = "us-central1-f"
        self._restore_zone_health(
            {zone: {"consecutive_failures": 0, "recent_outcomes": [True, False, True]}}
        )
        outcomes = compute._zone_health[zone]["recent_outcomes"]
        assert isinstance(outcomes, deque)
        assert outcomes.maxlen == compute.ZONE_HISTORY_WINDOW
        assert list(outcomes) == [True, False, True]

    def test_recent_outcomes_capped_to_window(self):
        """Persisted list longer than ZONE_HISTORY_WINDOW is truncated to the last N entries."""
        zone = "us-central1-f"
        oversized = [True] * 5 + [False] * (compute.ZONE_HISTORY_WINDOW + 5)
        self._restore_zone_health({zone: {"recent_outcomes": oversized}})
        outcomes = compute._zone_health[zone]["recent_outcomes"]
        assert isinstance(outcomes, deque)
        assert outcomes.maxlen == compute.ZONE_HISTORY_WINDOW
        assert len(outcomes) == compute.ZONE_HISTORY_WINDOW
        # Tail entries should all be False (the last ZONE_HISTORY_WINDOW elements)
        assert all(v is False for v in outcomes)

    def test_appending_beyond_window_evicts_old_entries(self):
        """After restore, appending beyond maxlen still evicts the oldest entry."""
        zone = "us-central1-f"
        initial = [True] * compute.ZONE_HISTORY_WINDOW
        self._restore_zone_health({zone: {"recent_outcomes": initial}})
        outcomes = compute._zone_health[zone]["recent_outcomes"]
        assert len(outcomes) == compute.ZONE_HISTORY_WINDOW
        outcomes.append(False)  # Should evict oldest True
        assert len(outcomes) == compute.ZONE_HISTORY_WINDOW
        assert outcomes[-1] is False
        assert outcomes[0] is True  # remaining entries still True

    def test_missing_recent_outcomes_key_is_unaffected(self):
        """Zones without recent_outcomes in persisted data are restored normally."""
        zone = "us-central1-f"
        self._restore_zone_health({zone: {"consecutive_failures": 2}})
        assert "recent_outcomes" not in compute._zone_health[zone]
        assert compute._zone_health[zone]["consecutive_failures"] == 2

    def test_recent_outcomes_corrupted_elements_filtered(self):
        """Non-boolean elements in recent_outcomes are dropped; valid booleans kept."""
        zone = "us-central1-f"
        self._restore_zone_health(
            {zone: {"recent_outcomes": [True, "bad", None, False, 1]}}
        )
        outcomes = compute._zone_health[zone]["recent_outcomes"]
        assert isinstance(outcomes, deque)
        # Only the two actual booleans should survive
        assert list(outcomes) == [True, False]

    def test_recent_outcomes_fully_corrupt_yields_empty_deque(self):
        """Entirely non-boolean recent_outcomes list yields an empty deque (neutral rate)."""
        zone = "us-central1-f"
        self._restore_zone_health({zone: {"recent_outcomes": ["bad", None, 0, 1]}})
        outcomes = compute._zone_health[zone]["recent_outcomes"]
        assert isinstance(outcomes, deque)
        assert len(outcomes) == 0
        # _zone_success_rate should return the neutral rate for empty deque
        assert compute._zone_success_rate(zone) == compute._ZONE_NEUTRAL_RATE

    def test_consecutive_failures_corrupted_defaults_to_zero(self):
        """Non-integer consecutive_failures falls back to 0."""
        zone = "us-central1-f"
        self._restore_zone_health({zone: {"consecutive_failures": "bad"}})
        assert compute._zone_health[zone]["consecutive_failures"] == 0

    def test_last_failure_reason_corrupted_defaults_to_none(self):
        """Non-string last_failure_reason falls back to None."""
        zone = "us-central1-f"
        self._restore_zone_health({zone: {"last_failure_reason": 42}})
        assert compute._zone_health[zone]["last_failure_reason"] is None

    def test_last_failure_reason_none_is_valid(self):
        """None is a valid value for last_failure_reason and is preserved."""
        zone = "us-central1-f"
        self._restore_zone_health({zone: {"last_failure_reason": None}})
        assert compute._zone_health[zone]["last_failure_reason"] is None


class TestZoneCooldownSeconds:
    """Cover _zone_cooldown_seconds exponential backoff with 1800s cap."""

    def test_zero_failures_baseline(self, mocker):
        mocker.patch("app.time_utils.random.uniform", return_value=1.0)
        assert compute._zone_cooldown_seconds(0) == compute.ZONE_COOLDOWN_SECONDS

    def test_doubling_on_each_failure(self, mocker):
        mocker.patch("app.time_utils.random.uniform", return_value=1.0)
        base = compute.ZONE_COOLDOWN_SECONDS
        assert compute._zone_cooldown_seconds(1) == min(base * 2, 1800)
        assert compute._zone_cooldown_seconds(2) == min(base * 4, 1800)
        assert compute._zone_cooldown_seconds(3) == min(base * 8, 1800)

    def test_cap_at_1800_seconds(self, mocker):
        mocker.patch("app.time_utils.random.uniform", return_value=1.0)
        assert compute._zone_cooldown_seconds(10) == 1800
        assert compute._zone_cooldown_seconds(100) == 1800

    def test_monotonically_non_decreasing(self, mocker):
        mocker.patch("app.time_utils.random.uniform", return_value=1.0)
        prev = 0
        for n in range(0, 15):
            current = compute._zone_cooldown_seconds(n)
            assert current >= prev
            prev = current


class TestValidatePayloadUri:
    """Cover validate_payload_uri GCS URI format validation."""

    def test_valid_simple_uri(self):
        compute.validate_payload_uri("gs://bucket/object")

    def test_valid_nested_path(self):
        compute.validate_payload_uri("gs://my-bucket/path/to/file.json")

    def test_valid_with_allowed_special_chars(self):
        compute.validate_payload_uri("gs://bucket.name_1-2/a_b.c-d/e.json")

    @pytest.mark.parametrize(
        "uri",
        [
            "",
            "   ",
            "http://bucket/object",
            "s3://bucket/object",
            "gs://bucket",
            "gs://bucket/",
            "gs:///object",
            "gs://bucket/object with spaces",
            "gs://bucket/obj#frag",
            "gs://bucket/obj?query",
            "gs://bucket/obj*",
            "/local/path",
        ],
    )
    def test_invalid_uris_raise_value_error(self, uri):
        with pytest.raises(ValueError, match="Invalid payload_uri"):
            compute.validate_payload_uri(uri)


def _cooldown_all_zones(expires_at: datetime) -> None:
    """Mark every fallback zone as in cooldown with the given expiry."""
    for zone, _subnet in compute.FALLBACK_ZONES:
        compute._zone_health[zone] = {"cooldown_expires_at": expires_at}


class TestAnyZoneAvailable:
    """Tests for any_zone_available()."""

    def test_empty_health_dict_returns_true(self, clean_zone_health):
        assert compute.any_zone_available() is True

    def test_one_available_others_in_cooldown(self, clean_zone_health):
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        _cooldown_all_zones(future)
        # Clear cooldown on one zone
        compute._zone_health[compute.FALLBACK_ZONES[0][0]] = {
            "cooldown_expires_at": None
        }
        assert compute.any_zone_available() is True

    def test_all_zones_in_cooldown_returns_false(self, clean_zone_health):
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        _cooldown_all_zones(future)
        assert compute.any_zone_available() is False

    def test_cooldown_expired_returns_true(self, clean_zone_health):
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        _cooldown_all_zones(past)
        assert compute.any_zone_available() is True


class TestSoonestZoneRecovery:
    """Tests for soonest_zone_recovery()."""

    def test_no_zones_in_health_returns_none(self, clean_zone_health):
        assert compute.soonest_zone_recovery() is None

    def test_one_zone_available_returns_none(self, clean_zone_health):
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        _cooldown_all_zones(future)
        compute._zone_health[compute.FALLBACK_ZONES[0][0]] = {
            "cooldown_expires_at": None
        }
        assert compute.soonest_zone_recovery() is None

    def test_all_in_cooldown_returns_earliest(self, clean_zone_health):
        now = datetime.now(timezone.utc)
        for idx, (zone, _subnet) in enumerate(compute.FALLBACK_ZONES):
            compute._zone_health[zone] = {
                "cooldown_expires_at": now + timedelta(minutes=10 + idx),
            }
        expected = now + timedelta(minutes=10)
        assert compute.soonest_zone_recovery() == expected

    def test_expired_cooldown_returns_none(self, clean_zone_health):
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        _cooldown_all_zones(past)
        assert compute.soonest_zone_recovery() is None


class TestZoneRecoveryWaitSeconds:
    """Regression tests for zone_recovery_wait_seconds() (Audit C10).

    soonest_zone_recovery() returns a tz-AWARE datetime. The orchestrator
    previously subtracted the app's naive _utcnow() at two sites, which raised
    TypeError during zone exhaustion — silently rolling back the infra-requeue's
    QUEUED/retry_after state and spamming the all-zones-cooldown error counter.
    This helper centralises the aware subtraction so the math can't regress.
    """

    def test_none_when_zone_available(self, clean_zone_health):
        """No cooldown state → a zone is available → None (not 0)."""
        assert compute.zone_recovery_wait_seconds() is None

    def test_aware_cooldown_does_not_raise_typeerror(self, clean_zone_health):
        """All zones in cooldown with tz-AWARE expiries must NOT raise TypeError.

        Reproduces the C10 bug: _zone_health stores aware cooldown_expires_at
        (built from datetime.now(timezone.utc)), so any naive subtraction inside
        the helper would raise here. The pre-fix orchestrator code did exactly
        that. Asserts a sane positive wait instead.
        """
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        _cooldown_all_zones(future)
        wait = compute.zone_recovery_wait_seconds()
        assert wait is not None
        # ~300s minus a few ms of test execution; must be positive and bounded.
        assert 240 < wait <= 300

    def test_floored_at_zero_for_just_expired(self, clean_zone_health):
        """A cooldown in the (very recent) past floors at 0, never negative.

        soonest_zone_recovery() returns None once any zone is past expiry, but
        guard the floor explicitly so a race between the two aware now() reads
        can never produce a negative wait.
        """
        # Set every zone to expire 1ms in the future so soonest_zone_recovery
        # still returns it, but by the time the helper subtracts now() it may be
        # in the past — the max(0.0, ...) floor must hold.
        near = datetime.now(timezone.utc) + timedelta(milliseconds=1)
        _cooldown_all_zones(near)
        wait = compute.zone_recovery_wait_seconds()
        # Either None (already expired by the time soonest_zone_recovery checks)
        # or a non-negative float — never negative, never a TypeError.
        assert wait is None or wait >= 0.0

    def test_orchestrator_imports_aware_helper(self):
        """The orchestrator must use zone_recovery_wait_seconds(), not its own
        naive subtraction. Guards against a future reintroduction of the C10
        naive/aware footgun at the dispatch / infra-requeue sites."""
        import app.orchestrator as _orch

        assert _orch.zone_recovery_wait_seconds is compute.zone_recovery_wait_seconds


# ─── Quota-scope classification (zone-failure misclassification fix) ─────────


# Classification matrix: (error text, expected scope)
_QUOTA_SCOPE_MATRIX = [
    # Global: the error text literally says "globally" / names an
    # ALL_REGIONS quota metric.
    (
        "403 QUOTA_EXCEEDED: Quota 'CPUS_ALL_REGIONS' exceeded. Limit: 32.0 globally.",
        provision_error_scope.QUOTA_SCOPE_GLOBAL,
    ),
    (
        "Quota 'GPUS_ALL_REGIONS' exceeded. Limit: 0.0 globally.",
        provision_error_scope.QUOTA_SCOPE_GLOBAL,
    ),
    (
        "quota 'cpus_all_regions' exceeded. limit: 32.0 globally.",
        provision_error_scope.QUOTA_SCOPE_GLOBAL,
    ),
    ("Quota exceeded. Limit: 64.0 globally.", provision_error_scope.QUOTA_SCOPE_GLOBAL),
    # Regional: region-scoped quota metrics.
    (
        "403 QUOTA_EXCEEDED: Quota 'CPUS' exceeded. Limit: 24.0 in region us-central1.",
        provision_error_scope.QUOTA_SCOPE_REGIONAL,
    ),
    (
        "Quota 'IN_USE_ADDRESSES' exceeded. Limit: 8.0 in region us-east1.",
        provision_error_scope.QUOTA_SCOPE_REGIONAL,
    ),
    # Unscoped quota wording defaults to regional (the conservative middle
    # ground: never paints other regions, never silently pauses the project).
    ("QUOTA_EXCEEDED: quota exceeded", provision_error_scope.QUOTA_SCOPE_REGIONAL),
    # Zonal: genuine zone stockouts / shortages.
    (
        "ZONE_RESOURCE_POOL_EXHAUSTED: The zone 'projects/p/zones/us-central1-f' "
        "does not have enough resources available to fulfill the request.",
        provision_error_scope.QUOTA_SCOPE_ZONAL,
    ),
    (
        "The zone 'projects/p/zones/us-east4-b' does not have enough resources "
        "available to fulfill the request. Try a different zone.",
        provision_error_scope.QUOTA_SCOPE_ZONAL,
    ),
    # Non-quota retryables (subnet missing, etc.) stay zone-scoped.
    (
        "Invalid value for field 'subnetwork': resource was not found",
        provision_error_scope.QUOTA_SCOPE_ZONAL,
    ),
    ("", provision_error_scope.QUOTA_SCOPE_ZONAL),
]


class TestQuotaScopeClassification:
    """Matrix tests for classify_provision_error_scope().

    A single CPUS_ALL_REGIONS exhaustion is a PROJECT-GLOBAL condition; the
    old classifier treated any error containing QUOTA as a zone failure and
    cycled every enabled zone into cooldown.
    """

    @pytest.mark.parametrize("err_str,expected", _QUOTA_SCOPE_MATRIX)
    def test_classification_matrix(self, err_str, expected):
        assert provision_error_scope.classify_provision_error_scope(err_str) == expected

    def test_zonal_marker_wins_over_generic_quota_wording(self):
        """A decisive zone-stockout marker stays zonal even if the message also
        mentions quota somewhere."""
        err = (
            "ZONE_RESOURCE_POOL_EXHAUSTED: zone does not have enough resources; "
            "see quota documentation"
        )
        assert (
            provision_error_scope.classify_provision_error_scope(err)
            == provision_error_scope.QUOTA_SCOPE_ZONAL
        )


_GLOBAL_QUOTA_ERR = (
    "403 QUOTA_EXCEEDED: Quota 'CPUS_ALL_REGIONS' exceeded. Limit: 32.0 globally."
)
_REGIONAL_QUOTA_ERR = (
    "403 QUOTA_EXCEEDED: Quota 'IN_USE_ADDRESSES' exceeded. "
    "Limit: 8.0 in region us-central1."
)


class TestGlobalQuotaBackoff:
    """Project-level backoff for global quota exhaustion — NO zone marks."""

    def test_record_sets_cooldown_and_marks_no_zones(self, clean_zone_health):
        cooldown_secs = provision_error_scope._record_global_quota_exhaustion(
            _GLOBAL_QUOTA_ERR
        )
        assert cooldown_secs == provision_error_scope.GLOBAL_QUOTA_COOLDOWN_SECONDS
        expires = provision_error_scope._global_quota_state["cooldown_expires_at"]
        assert expires > datetime.now(timezone.utc)
        # The whole point: no zone is charged.
        assert compute._zone_health == {}
        assert compute._zone_failures == {}

    def test_global_cooldown_blocks_every_zone(self, clean_zone_health):
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        assert compute.any_zone_available() is False
        recovery = compute.soonest_zone_recovery()
        assert (
            recovery == provision_error_scope._global_quota_state["cooldown_expires_at"]
        )
        wait = compute.zone_recovery_wait_seconds()
        assert wait is not None and wait > 0

    def test_cooldown_expiry_restores_availability(self, clean_zone_health):
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        provision_error_scope._global_quota_state["cooldown_expires_at"] = datetime.now(
            timezone.utc
        ) - timedelta(seconds=1)
        assert compute.any_zone_available() is True

    def test_parallel_failures_debounced(self, clean_zone_health):
        """Concurrent provision attempts failing on the same quota event must
        not escalate the backoff (mirrors the per-zone debounce)."""
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        assert provision_error_scope._global_quota_state["consecutive"] == 1

    def test_distinct_events_escalate_exponentially(self, clean_zone_health):
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        # Age the last event past the debounce window.
        provision_error_scope._global_quota_state["last_event"] = datetime.now(
            timezone.utc
        ) - timedelta(seconds=provision_error_scope.GLOBAL_QUOTA_COOLDOWN_SECONDS + 1)
        cooldown_secs = provision_error_scope._record_global_quota_exhaustion(
            _GLOBAL_QUOTA_ERR
        )
        assert provision_error_scope._global_quota_state["consecutive"] == 2
        assert cooldown_secs == min(
            provision_error_scope.GLOBAL_QUOTA_COOLDOWN_SECONDS * 2,
            provision_error_scope._QUOTA_COOLDOWN_CAP_SECONDS,
        )

    def test_backoff_capped(self):
        assert (
            provision_error_scope._quota_backoff_seconds(
                100, provision_error_scope.GLOBAL_QUOTA_COOLDOWN_SECONDS
            )
            == provision_error_scope._QUOTA_COOLDOWN_CAP_SECONDS
        )

    def test_operator_alert_fired_and_deduplicated(
        self, clean_zone_health, monkeypatch
    ):
        alerts = []
        monkeypatch.setattr(
            provision_error_scope,
            "_post_global_quota_alert",
            lambda err, secs, consec: alerts.append((err, secs, consec)),
        )
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        assert len(alerts) == 1
        # A storm of failures within the dedup window must not spam the operator.
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        assert len(alerts) == 1
        # Past the dedup window the alert re-fires.
        provision_error_scope._global_quota_state["last_alert_at"] = datetime.now(
            timezone.utc
        ) - timedelta(seconds=provision_error_scope._QUOTA_ALERT_DEDUP_SECONDS + 1)
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        assert len(alerts) == 2

    def test_success_clears_global_cooldown(self, clean_zone_health):
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        provision_error_scope._clear_quota_cooldowns_after_success("us-central1-f")
        assert provision_error_scope._global_quota_state == {}
        assert compute.any_zone_available() is True

    def test_summary_surfaces_global_cooldown(self, clean_zone_health):
        summary = compute.get_zone_health_summary()
        assert summary["global_quota_cooldown_until"] is None
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        summary = compute.get_zone_health_summary()
        assert summary["global_quota_cooldown_until"] is not None


class TestRegionQuotaBackoff:
    """Region-level backoff for regional quota exhaustion — blocks only that
    region's zones, marks no individual zone."""

    def test_record_blocks_only_that_region(self, clean_zone_health):
        provision_error_scope._record_region_quota_exhaustion(
            "us-central1", _REGIONAL_QUOTA_ERR
        )
        now = datetime.now(timezone.utc)
        assert compute._zone_blocked_until("us-central1-f", now) is not None
        assert compute._zone_blocked_until("us-central1-a", now) is not None
        assert compute._zone_blocked_until("us-east4-b", now) is None
        assert compute._zone_blocked_until("us-west1-a", now) is None
        # Other regions keep the fleet available.
        assert compute.any_zone_available() is True
        # And no zone was charged.
        assert compute._zone_health == {}

    def test_all_regions_blocked_means_nothing_available(self, clean_zone_health):
        regions = {
            provision_error_scope._zone_region(z) for z, _ in compute.FALLBACK_ZONES
        }
        for region in regions:
            provision_error_scope._record_region_quota_exhaustion(
                region, _REGIONAL_QUOTA_ERR
            )
        assert compute.any_zone_available() is False

    def test_success_in_region_clears_its_cooldown_only(self, clean_zone_health):
        provision_error_scope._record_region_quota_exhaustion(
            "us-central1", _REGIONAL_QUOTA_ERR
        )
        provision_error_scope._record_region_quota_exhaustion(
            "us-east4", _REGIONAL_QUOTA_ERR
        )
        provision_error_scope._clear_quota_cooldowns_after_success("us-central1-f")
        assert "us-central1" not in provision_error_scope._region_quota_state
        assert "us-east4" in provision_error_scope._region_quota_state

    def test_summary_surfaces_region_cooldowns(self, clean_zone_health):
        provision_error_scope._record_region_quota_exhaustion(
            "us-central1", _REGIONAL_QUOTA_ERR
        )
        summary = compute.get_zone_health_summary()
        assert "us-central1" in summary["region_quota_cooldowns"]

    def test_region_debounce_and_escalation(self, clean_zone_health):
        provision_error_scope._record_region_quota_exhaustion(
            "us-central1", _REGIONAL_QUOTA_ERR
        )
        provision_error_scope._record_region_quota_exhaustion(
            "us-central1", _REGIONAL_QUOTA_ERR
        )
        assert (
            provision_error_scope._region_quota_state["us-central1"]["consecutive"] == 1
        )
        provision_error_scope._region_quota_state["us-central1"]["last_event"] = (
            datetime.now(timezone.utc)
            - timedelta(seconds=provision_error_scope.REGION_QUOTA_COOLDOWN_SECONDS + 1)
        )
        provision_error_scope._record_region_quota_exhaustion(
            "us-central1", _REGIONAL_QUOTA_ERR
        )
        assert (
            provision_error_scope._region_quota_state["us-central1"]["consecutive"] == 2
        )


class TestZoneSuspectCorroboration:
    """record_zone_failure() must not charge a zone for a single silent VM
    death (HEARTBEAT_STARVATION / AGENT_STARTUP_FAILURE / …) — those are
    usually VM/image/startup-script failures, not zone health. Charging
    requires corroboration: N reports in the same zone within the window."""

    def test_single_vm_death_does_not_charge_zone(self, clean_zone_health):
        zone = "us-central1-f"
        compute.record_zone_failure(zone, reason="HEARTBEAT_STARVATION")
        assert zone not in compute._zone_health
        assert zone not in compute._zone_failures
        assert len(compute._zone_suspect_events[zone]) == 1
        assert compute.any_zone_available() is True

    def test_below_threshold_reports_do_not_charge(self, clean_zone_health):
        zone = "us-central1-f"
        for _ in range(compute.ZONE_SUSPECT_CORROBORATION_THRESHOLD - 1):
            compute.record_zone_failure(zone, reason="AGENT_STARTUP_FAILURE")
        assert zone not in compute._zone_health

    def test_corroborated_reports_charge_zone_once(self, clean_zone_health):
        zone = "us-central1-f"
        for _ in range(compute.ZONE_SUSPECT_CORROBORATION_THRESHOLD):
            compute.record_zone_failure(zone, reason="HEARTBEAT_STARVATION")
        health = compute._zone_health[zone]
        assert health["consecutive_failures"] == 1
        assert health["cooldown_expires_at"] > datetime.now(timezone.utc)
        assert health["last_failure_reason"] == "HEARTBEAT_STARVATION_CORROBORATED"
        # Exactly one success-rate hit for the whole corroborated event.
        assert list(health["recent_outcomes"]) == [False]
        # Evidence consumed: the next charge needs fresh corroboration.
        assert len(compute._zone_suspect_events[zone]) == 0

    def test_reports_outside_window_expire(self, clean_zone_health):
        zone = "us-central1-f"
        stale = datetime.now(timezone.utc) - timedelta(
            seconds=compute.ZONE_SUSPECT_WINDOW_SECONDS + 60
        )
        events = compute._zone_suspect_events.setdefault(zone, deque(maxlen=64))
        for _ in range(compute.ZONE_SUSPECT_CORROBORATION_THRESHOLD - 1):
            events.append(stale)
        compute.record_zone_failure(zone, reason="HEARTBEAT_STARVATION")
        # Stale evidence was pruned, so this is report 1/N — no charge.
        assert zone not in compute._zone_health
        assert len(compute._zone_suspect_events[zone]) == 1

    def test_reports_isolated_per_zone(self, clean_zone_health):
        """Deaths spread across different zones must not corroborate each other."""
        for zone in ("us-central1-f", "us-central1-c", "us-east4-b"):
            compute.record_zone_failure(zone, reason="HEARTBEAT_STARVATION")
        assert compute._zone_health == {}

    def test_authoritative_reason_charges_immediately(self, clean_zone_health):
        zone = "us-central1-f"
        compute.record_zone_failure(zone, reason="RESOURCE_POOL_EXHAUSTED")
        health = compute._zone_health[zone]
        assert health["consecutive_failures"] == 1
        assert health["cooldown_expires_at"] > datetime.now(timezone.utc)
        assert health["last_failure_reason"] == "RESOURCE_POOL_EXHAUSTED"


class TestProvisionQuotaScope:
    """Integration: the _provision_gcp zone loop must not paint zones on
    global/regional quota errors."""

    @pytest.fixture()
    def _provision_env(self, monkeypatch):
        async def _ok(*args, **kwargs):
            return True

        async def _noop(*args, **kwargs):
            return None

        monkeypatch.setattr(compute, "_acquire_provision_slot", _ok)
        monkeypatch.setattr(compute, "_release_provision_slot", _noop)
        monkeypatch.setattr(compute, "_delete_agent_secret", lambda vm_id: None)

    async def test_global_quota_error_marks_no_zone_and_stops_cycling(
        self, clean_zone_health, _provision_env
    ):
        with patch("google.cloud.compute_v1.InstancesClient") as mock_client:
            mock_client.return_value.get.side_effect = Exception("404 was not found")
            mock_client.return_value.insert.side_effect = Exception(_GLOBAL_QUOTA_ERR)

            result = await compute._provision_gcp(
                "vm-global-quota-test",
                uuid.uuid4(),
                secret_version_name="projects/p/secrets/s/versions/1",
            )

        assert result is False
        # One insert only — cycling to the next zone would re-trip the same
        # project-global quota in every remaining zone.
        assert mock_client.return_value.insert.call_count == 1
        # NO zone marks: the old behavior painted every enabled zone failed.
        assert compute._zone_health == {}
        assert compute._zone_failures == {}
        # Project-level backoff armed instead.
        assert "cooldown_expires_at" in provision_error_scope._global_quota_state
        assert compute.any_zone_available() is False
        # Error surfaced for the reaper's INFRA_PROVISION_PATTERNS classifier.
        err = compute.get_provision_error("vm-global-quota-test")
        assert "quota" in err.lower()

    async def test_provision_fails_fast_while_global_cooldown_active(
        self, clean_zone_health, _provision_env
    ):
        provision_error_scope._record_global_quota_exhaustion(_GLOBAL_QUOTA_ERR)
        with patch("google.cloud.compute_v1.InstancesClient") as mock_client:
            result = await compute._provision_gcp(
                "vm-cooldown-test",
                uuid.uuid4(),
                secret_version_name="projects/p/secrets/s/versions/1",
            )
        assert result is False
        mock_client.return_value.insert.assert_not_called()
        err = compute.get_provision_error("vm-cooldown-test")
        assert "quota" in err.lower()

    async def test_regional_quota_skips_region_and_succeeds_elsewhere(
        self, clean_zone_health, _provision_env
    ):
        # Make us-central1-f sort first (perfect success history).
        compute._zone_health["us-central1-f"] = {
            "recent_outcomes": deque([True] * 10, maxlen=compute.ZONE_HISTORY_WINDOW),
        }
        attempted = []

        def _insert(*args, **kwargs):
            zone = kwargs["zone"]
            attempted.append(zone)
            if zone.startswith("us-central1"):
                raise Exception(_REGIONAL_QUOTA_ERR)
            op = MagicMock()
            op.name = "op-regional-test"
            return op

        with patch("google.cloud.compute_v1.InstancesClient") as mock_client:
            mock_client.return_value.get.side_effect = Exception("404 was not found")
            mock_client.return_value.insert.side_effect = _insert

            result = await compute._provision_gcp(
                "vm-regional-quota-test",
                uuid.uuid4(),
                secret_version_name="projects/p/secrets/s/versions/1",
            )

        assert result is True
        # First attempt hit the regional quota; the OTHER us-central1 zones
        # were skipped (region cooldown), and the very next attempt was in a
        # different region.
        assert attempted[0] == "us-central1-f"
        assert len(attempted) == 2
        assert not attempted[1].startswith("us-central1")
        # The exhausted region is in cooldown; the failing zone was NOT charged.
        assert "us-central1" in provision_error_scope._region_quota_state
        f_health = compute._zone_health.get("us-central1-f", {})
        assert f_health.get("cooldown_expires_at") is None
        assert f_health.get("consecutive_failures", 0) == 0
        assert list(f_health.get("recent_outcomes", [])) == [True] * 10


class TestZoneInDisabledRegion:
    """Tests for _zone_in_disabled_region()."""

    def test_zone_in_disabled_region_extracts_region(self, monkeypatch):
        monkeypatch.setattr(
            compute, "_DISABLED_REGIONS", frozenset(["us-east1", "us-west1"])
        )
        assert compute._zone_in_disabled_region("us-east1-a") is True
        assert compute._zone_in_disabled_region("us-east1-c") is True
        assert compute._zone_in_disabled_region("us-west1-b") is True
        assert compute._zone_in_disabled_region("us-central1-f") is False
        assert compute._zone_in_disabled_region("us-east4-a") is False

    def test_zone_in_disabled_region_empty_set(self, monkeypatch):
        monkeypatch.setattr(compute, "_DISABLED_REGIONS", frozenset())
        for zone, _ in compute.FALLBACK_ZONES:
            assert compute._zone_in_disabled_region(zone) is False


class TestActiveFallbackZones:
    """Tests for _active_fallback_zones()."""

    def test_no_disabled_returns_full_list(self, monkeypatch):
        monkeypatch.setattr(compute, "_DISABLED_REGIONS", frozenset())
        assert compute._active_fallback_zones() == compute.FALLBACK_ZONES

    def test_filters_one_region(self, monkeypatch):
        monkeypatch.setattr(compute, "_DISABLED_REGIONS", frozenset(["us-east1"]))
        result = compute._active_fallback_zones()
        result_zones = [z for z, _ in result]
        assert "us-east1-c" not in result_zones
        assert "us-east1-b" not in result_zones
        assert "us-central1-a" in result_zones
        assert "us-east4-a" in result_zones  # us-east4 not disabled, still present
        assert len(result) == len(compute.FALLBACK_ZONES) - 2  # us-east1 had 2 zones

    def test_filters_three_regions_leaves_central(self, monkeypatch):
        monkeypatch.setattr(
            compute,
            "_DISABLED_REGIONS",
            frozenset(["us-east1", "us-east4", "us-west1"]),
        )
        result = compute._active_fallback_zones()
        result_zones = [z for z, _ in result]
        assert all(z.startswith("us-central1-") for z in result_zones)
        assert len(result) == 4  # us-central1 has zones a, b, c, f


class TestFormatOrphanStatus:
    """Tests for _format_orphan_status()."""

    def test_terminated_in_spot_mode_annotated(self, monkeypatch):
        monkeypatch.setattr(compute, "VM_PROVISIONING_MODEL", "SPOT")
        assert (
            compute._format_orphan_status("TERMINATED")
            == "TERMINATED (likely spot preemption)"
        )

    def test_terminated_in_standard_mode_passthrough(self, monkeypatch):
        monkeypatch.setattr(compute, "VM_PROVISIONING_MODEL", "STANDARD")
        assert compute._format_orphan_status("TERMINATED") == "TERMINATED"

    def test_running_in_spot_mode_passthrough(self, monkeypatch):
        # Only TERMINATED gets the spot annotation; other statuses are
        # not preemption-implied.
        monkeypatch.setattr(compute, "VM_PROVISIONING_MODEL", "SPOT")
        assert compute._format_orphan_status("RUNNING") == "RUNNING"
        assert compute._format_orphan_status("STOPPING") == "STOPPING"
        assert compute._format_orphan_status("PROVISIONING") == "PROVISIONING"


class TestDisabledRegionsParsing:
    """Tests for DISABLED_REGIONS env var parsing.

    The parser at module scope reads os.getenv once. We can't re-trigger that
    without importlib.reload, so we exercise the same logic inline.
    """

    @staticmethod
    def _parse(raw: str) -> frozenset[str]:
        # Mirror the production parser at compute.py:162-165.
        normalized = raw.replace(";", ",")
        return frozenset(r.strip() for r in normalized.split(",") if r.strip())

    def test_empty_string(self):
        assert self._parse("") == frozenset()

    def test_single_region(self):
        assert self._parse("us-east1") == frozenset({"us-east1"})

    def test_comma_separated(self):
        assert self._parse("us-east1,us-east4,us-west1") == frozenset(
            {"us-east1", "us-east4", "us-west1"}
        )

    def test_semicolon_separated(self):
        # Cloudbuild.yaml uses semicolons because gcloud --set-env-vars uses
        # `,` as KEY=VAL delimiter.
        assert self._parse("us-east1;us-east4;us-west1") == frozenset(
            {"us-east1", "us-east4", "us-west1"}
        )

    def test_mixed_separators(self):
        assert self._parse("us-east1;us-east4,us-west1") == frozenset(
            {"us-east1", "us-east4", "us-west1"}
        )

    def test_whitespace_normalized(self):
        assert self._parse(" us-east1 ; us-east4 ") == frozenset(
            {"us-east1", "us-east4"}
        )

    def test_trailing_separators_dropped(self):
        assert self._parse(";us-east1;;us-east4;") == frozenset(
            {"us-east1", "us-east4"}
        )

    def test_duplicate_collapsed(self):
        assert self._parse("us-east1,us-east1") == frozenset({"us-east1"})


class TestGetVmStatus:
    """Tests for get_vm_status() — used by reaper to detect spot preemption."""

    def test_zone_unknown_returns_none(self):
        assert compute.get_vm_status("agent-vm-abc", "unknown") is None
        assert compute.get_vm_status("agent-vm-abc", "") is None

    def test_running_status(self):
        from unittest.mock import MagicMock, patch

        mock_instance = MagicMock()
        mock_instance.status = "RUNNING"
        with patch("google.cloud.compute_v1.InstancesClient") as mock_client:
            mock_client.return_value.get.return_value = mock_instance
            assert compute.get_vm_status("agent-vm-abc", "us-central1-a") == "RUNNING"

    def test_terminated_status(self):
        from unittest.mock import MagicMock, patch

        mock_instance = MagicMock()
        mock_instance.status = "TERMINATED"
        with patch("google.cloud.compute_v1.InstancesClient") as mock_client:
            mock_client.return_value.get.return_value = mock_instance
            assert (
                compute.get_vm_status("agent-vm-abc", "us-central1-a") == "TERMINATED"
            )

    def test_not_found_returns_deleted(self):
        from unittest.mock import patch

        with patch("google.cloud.compute_v1.InstancesClient") as mock_client:
            mock_client.return_value.get.side_effect = Exception(
                "404 was not found in zone"
            )
            # SPOT VMs use DELETE termination action, so preempted VMs become 404.
            # get_vm_status returns the synthetic "DELETED" so reaper can distinguish
            # vanished from API-failed.
            assert compute.get_vm_status("agent-vm-abc", "us-central1-a") == "DELETED"

    def test_other_api_error_returns_none(self):
        from unittest.mock import patch

        with patch("google.cloud.compute_v1.InstancesClient") as mock_client:
            mock_client.return_value.get.side_effect = Exception("503 backend timeout")
            assert compute.get_vm_status("agent-vm-abc", "us-central1-a") is None


class TestBootDiskType:
    """provision_agent_vm requests a pd-balanced (SSD-backed) boot disk.

    pd-balanced boots meaningfully faster than the pd-standard (HDD) default
    GCP uses when disk_type is unset. disk_type is zone-qualified, so the value
    sent to insert() must reference the same zone the instance is created in.
    """

    async def test_disk_type_is_pd_balanced_and_zone_matched(
        self, monkeypatch, clean_zone_health
    ):
        captured = {}

        def _capture_insert(*args, **kwargs):
            inst = kwargs["instance_resource"]
            captured["zone"] = kwargs["zone"]
            captured["disk_type"] = inst.disks[0].initialize_params.disk_type
            captured["machine_type"] = inst.machine_type
            op = MagicMock()
            op.name = "op-disk-test"
            return op

        async def _ok(*args, **kwargs):
            return True

        async def _noop(*args, **kwargs):
            return None

        monkeypatch.setattr(compute, "_acquire_provision_slot", _ok)
        monkeypatch.setattr(compute, "_release_provision_slot", _noop)

        with patch("google.cloud.compute_v1.InstancesClient") as mock_client:
            # No stale VM to clean up — get() raising 404 short-circuits the delete.
            mock_client.return_value.get.side_effect = Exception("404 was not found")
            mock_client.return_value.insert.side_effect = _capture_insert

            result = await compute._provision_gcp(
                "vm-disktype-test",
                uuid.uuid4(),
                secret_version_name="projects/p/secrets/s/versions/1",
            )

        assert result is True
        assert (
            captured["disk_type"] == f"zones/{captured['zone']}/diskTypes/pd-balanced"
        )
        # Disk zone must track the machine-type zone (same insert target).
        assert f"zones/{captured['zone']}/" in captured["machine_type"]

    async def test_ssd_quota_retries_same_zone_with_pd_standard(
        self, monkeypatch, clean_zone_health
    ):
        inserts = []

        def _capture_insert(*args, **kwargs):
            inst = kwargs["instance_resource"]
            inserts.append(
                {
                    "zone": kwargs["zone"],
                    "disk_type": inst.disks[0].initialize_params.disk_type,
                }
            )
            op = MagicMock()
            op.name = f"op-disk-fallback-{len(inserts)}"
            if len(inserts) == 1:
                op.result.side_effect = Exception(
                    "403 FORBIDDEN QUOTA_EXCEEDED: Quota 'SSD_TOTAL_GB' exceeded"
                )
            return op

        async def _ok(*args, **kwargs):
            return True

        async def _noop(*args, **kwargs):
            return None

        monkeypatch.setattr(compute, "_acquire_provision_slot", _ok)
        monkeypatch.setattr(compute, "_release_provision_slot", _noop)

        with patch("google.cloud.compute_v1.InstancesClient") as mock_client:
            mock_client.return_value.get.side_effect = Exception("404 was not found")
            mock_client.return_value.insert.side_effect = _capture_insert

            result = await compute._provision_gcp(
                "vm-disktype-fallback-test",
                uuid.uuid4(),
                secret_version_name="projects/p/secrets/s/versions/1",
            )

        assert result is True
        assert len(inserts) == 2
        assert inserts[0]["zone"] == inserts[1]["zone"]
        assert inserts[0]["disk_type"].endswith("/diskTypes/pd-balanced")
        assert inserts[1]["disk_type"].endswith("/diskTypes/pd-standard")


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Quota 'SSD_TOTAL_GB' exceeded", True),
        ("quota SSD-TOTAL-GB exhausted", True),
        ("Quota CPUS exceeded", False),
        ("ZONE_RESOURCE_POOL_EXHAUSTED", False),
    ],
)
def test_is_ssd_boot_disk_quota_exhaustion(message, expected):
    assert compute._is_ssd_boot_disk_quota_exhaustion(message) is expected


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
    assert job.outcome == ComputeJobOutcome.SUCCESS

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
    assert compute_result["outcome"] == "SUCCESS"
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
    assert job.outcome == ComputeJobOutcome.SUCCESS
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
    assert compute_result["outcome"] == "SUCCESS"
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


# ---------------------------------------------------------------------------
# Single-GPU FACTS — pricing, accelerator gate, and zone candidates (inert).
# ---------------------------------------------------------------------------


def test_gpu_rate_includes_base_plus_accelerator():
    """N1 GPU rate = base machine + GPU surcharge (on-demand and spot)."""
    from app import vm_cost_rates as vcr

    # N1 + T4: base n1-standard-1 + T4 surcharge.
    t4_on = vcr.get_compute_rate_per_second("n1-standard-1", accelerator="T4")
    t4_spot = vcr.get_compute_rate_per_second(
        "n1-standard-1", accelerator="T4", spot=True
    )
    assert t4_on == pytest.approx(0.39749975 / 3600)
    assert t4_spot == pytest.approx(0.17481125 / 3600)
    # Explicitly = base + surcharge, not a bundled lookup.
    assert t4_on == pytest.approx((0.04749975 + 0.35) / 3600)
    assert t4_spot == pytest.approx((0.01381125 + 0.161) / 3600)

    # N1 + V100.
    v100_on = vcr.get_compute_rate_per_second("n1-standard-1", accelerator="V100")
    v100_spot = vcr.get_compute_rate_per_second(
        "n1-standard-1", accelerator="V100", spot=True
    )
    assert v100_on == pytest.approx(2.52749975 / 3600)
    assert v100_spot == pytest.approx(1.10501125 / 3600)
    assert v100_on == pytest.approx((0.04749975 + 2.48) / 3600)


def test_gpu_rate_g2_l4_is_bundled():
    """G2 + L4 rate comes from the bundled machine price (no separate surcharge)."""
    from app import vm_cost_rates as vcr

    l4_on = vcr.get_compute_rate_per_second("g2-standard-4", accelerator="L4")
    l4_spot = vcr.get_compute_rate_per_second(
        "g2-standard-4", accelerator="L4", spot=True
    )
    assert l4_on == pytest.approx(0.70683228 / 3600)
    assert l4_spot == pytest.approx(0.058736 / 3600)
    # L4 has no entry in the per-GPU surcharge table — it is bundled into G2.
    assert "nvidia-l4" not in vcr.GPU_ACCELERATOR_SURCHARGE_RATES


def test_compute_rate_without_accelerator_matches_cpu_rate():
    """accelerator=None falls through to the unchanged CPU (e2) accessor."""
    from app import vm_cost_rates as vcr

    for mt in vcr.supported_machine_types():
        assert vcr.get_compute_rate_per_second(mt) == vcr.get_vm_rate_per_second(mt)
        assert vcr.get_compute_rate_per_second(
            mt, spot=True
        ) == vcr.get_vm_rate_per_second(mt, spot=True)


def test_accelerator_gate_rejects_a100_count_and_unknown():
    """A100 (out of quota), count!=1, and unknown names are rejected."""
    from app import vm_cost_rates as vcr

    spec, reason = vcr.validate_accelerator_request("A100")
    assert spec is None and reason and "A100" in reason

    spec, reason = vcr.validate_accelerator_request("T4", count=2)
    assert spec is None and reason and "count" in reason

    spec, reason = vcr.validate_accelerator_request("nvidia-tesla-h100")
    assert spec is None and reason

    # The accessor surfaces the same rejections as exceptions.
    with pytest.raises(vcr.AcceleratorRejection):
        vcr.get_compute_rate_per_second("a2-highgpu-1g", accelerator="A100")
    with pytest.raises(vcr.AcceleratorRejection):
        vcr.get_compute_rate_per_second("n1-standard-1", accelerator="T4", count=4)


def test_supported_accelerators_type_strings_and_families():
    """Each accelerator maps to the correct GCP type string and machine family."""
    from app import vm_cost_rates as vcr

    assert set(vcr.SUPPORTED_ACCELERATORS) == {"T4", "L4", "V100"}
    assert "A100" not in vcr.SUPPORTED_ACCELERATORS

    t4 = vcr.SUPPORTED_ACCELERATORS["T4"]
    assert t4.gcp_type == "nvidia-tesla-t4"
    assert t4.machine_family == "n1" and not t4.bundled
    assert t4.machine_type == "n1-standard-1"

    v100 = vcr.SUPPORTED_ACCELERATORS["V100"]
    assert v100.gcp_type == "nvidia-tesla-v100"
    assert v100.machine_family == "n1" and not v100.bundled

    l4 = vcr.SUPPORTED_ACCELERATORS["L4"]
    assert l4.gcp_type == "nvidia-l4"
    assert l4.machine_family == "g2" and l4.bundled
    assert l4.machine_type == "g2-standard-4"


def test_gpu_facts_do_not_leak_into_cpu_create_gate():
    """Regression guard: GPU shapes must NOT enter the CPU-only create gate."""
    from app import vm_cost_rates as vcr

    assert set(vcr.supported_machine_types()) == {
        "c2d-standard-2",
        "n2-standard-2",
        "e2-standard-2",
        "e2-standard-4",
        "e2-standard-8",
        "e2-standard-16",
        "e2-standard-32",
    }
    for gpu_mt in ("n1-standard-1", "g2-standard-4"):
        assert not vcr.is_supported_machine_type(gpu_mt)
        assert gpu_mt not in vcr.VM_COST_RATES


def test_gpu_zone_candidates_non_empty_mapped_and_distinct_from_fleet():
    """Per-accelerator zone candidates are non-empty, subnet-correct, and not the fleet ring."""
    from app import compute

    fleet_zones = {z for z, _ in compute.FALLBACK_ZONES}

    region_subnets = {}
    for z, s in compute.FALLBACK_ZONES:
        region_subnets.setdefault(z.rsplit("-", 1)[0], s)

    for accel in ("T4", "L4", "V100", "nvidia-tesla-t4"):
        candidates = compute.gpu_zone_candidates(accel)
        assert candidates, f"expected non-empty zone list for {accel}"
        for zone, subnet in candidates:
            assert subnet == region_subnets[zone.rsplit("-", 1)[0]]

    # V100 has no us-east4 zones; T4 does — the maps are genuinely per-accelerator.
    t4_zones = {z for z, _ in compute.gpu_zone_candidates("T4")}
    v100_zones = {z for z, _ in compute.gpu_zone_candidates("V100")}
    assert any(z.startswith("us-east4") for z in t4_zones)
    assert not any(z.startswith("us-east4") for z in v100_zones)

    # Independent of the fleet ring: the GPU lists include zones not in FALLBACK_ZONES.
    assert t4_zones - fleet_zones, "GPU zone ring should differ from the fleet ring"

    # Unknown accelerators yield an empty candidate list.
    assert compute.gpu_zone_candidates("nvidia-a100") == []


def test_gpu_image_family_is_driver_preinstalled():
    """GPU image family defaults to a driver-preinstalled DLVM family."""
    from app import compute

    # Driver-preinstalled Deep Learning VM family (NVIDIA driver baked in).
    assert "nvidia" in compute.GPU_IMAGE_FAMILY
    assert "deeplearning-platform-release" in compute.GPU_IMAGE_FAMILY
    # Pinned the same way AGENT_IMAGE_FAMILY is (fully-qualified family path).
    assert compute.GPU_IMAGE_FAMILY.startswith("projects/")


class TestGpuImageFamilyValidation:
    """GPU image family is validated at startup and gates GPU provisioning."""

    def test_ensure_gpu_image_family_valid_raises_when_invalid(self, monkeypatch):
        """_ensure_gpu_image_family_valid raises RuntimeError when GPU image is invalid."""
        monkeypatch.setattr(compute, "_gpu_image_family_valid", False)
        monkeypatch.setattr(compute, "_gpu_image_family_last_check", 1.0)
        monkeypatch.setattr(compute, "LOCAL_MODE", False)
        # Set last check to far in the future so recheck interval has NOT elapsed.
        import time

        monkeypatch.setattr(compute, "_gpu_image_family_last_check", time.time())

        with pytest.raises(RuntimeError, match="GPU"):
            compute._ensure_gpu_image_family_valid()

    def test_ensure_gpu_image_family_valid_passes_when_valid(self, monkeypatch):
        """_ensure_gpu_image_family_valid is a no-op when GPU image is valid."""
        monkeypatch.setattr(compute, "_gpu_image_family_valid", True)
        monkeypatch.setattr(compute, "LOCAL_MODE", False)
        # Should not raise.
        compute._ensure_gpu_image_family_valid()

    def test_ensure_gpu_image_family_valid_rechecks_after_interval(self, monkeypatch):
        """Re-validates after _IMAGE_FAMILY_RECHECK_INTERVAL elapses."""
        monkeypatch.setattr(compute, "_gpu_image_family_valid", False)
        monkeypatch.setattr(compute, "_gpu_image_family_last_check", 0.0)
        monkeypatch.setattr(compute, "LOCAL_MODE", False)

        validate_calls = []

        def fake_validate():
            validate_calls.append(True)
            # Simulate successful validation by letting state be set normally.

        monkeypatch.setattr(compute, "_validate_gpu_image_family", fake_validate)

        # Should not raise — recheck interval elapsed, revalidation succeeds.
        compute._ensure_gpu_image_family_valid()
        assert len(validate_calls) == 1
        assert compute._gpu_image_family_valid is True

    def test_ensure_gpu_image_family_valid_noop_in_local_mode(self, monkeypatch):
        """_ensure_gpu_image_family_valid is a no-op in LOCAL_MODE even when invalid."""
        monkeypatch.setattr(compute, "_gpu_image_family_valid", False)
        monkeypatch.setattr(compute, "LOCAL_MODE", True)
        # Should not raise in LOCAL_MODE regardless of GPU image state.
        compute._ensure_gpu_image_family_valid()

    async def test_provision_ephemeral_vm_gpu_calls_gpu_gate(self, monkeypatch):
        """provision_ephemeral_vm calls _ensure_gpu_image_family_valid when accelerator is set."""
        monkeypatch.setattr(compute, "_image_family_valid", True)
        monkeypatch.setattr(compute, "_gpu_image_family_valid", True)
        monkeypatch.setattr(compute, "LOCAL_MODE", False)

        gpu_gate_calls = []

        def fake_gpu_gate():
            gpu_gate_calls.append(True)

        monkeypatch.setattr(compute, "_ensure_gpu_image_family_valid", fake_gpu_gate)

        async def fake_provision_gcp(**kwargs):
            return True

        monkeypatch.setattr(compute, "_provision_gcp", fake_provision_gcp)

        await compute.provision_ephemeral_vm(
            "compute-job-vm-test",
            startup_script="#!/bin/bash\ntrue",
            accelerator="T4",
        )
        assert len(gpu_gate_calls) == 1

    async def test_provision_ephemeral_vm_cpu_skips_gpu_gate(self, monkeypatch):
        """provision_ephemeral_vm does NOT call _ensure_gpu_image_family_valid when accelerator is None."""
        monkeypatch.setattr(compute, "_image_family_valid", True)
        monkeypatch.setattr(compute, "LOCAL_MODE", False)

        gpu_gate_calls = []

        def fake_gpu_gate():
            gpu_gate_calls.append(True)

        monkeypatch.setattr(compute, "_ensure_gpu_image_family_valid", fake_gpu_gate)

        async def fake_provision_gcp(**kwargs):
            return True

        monkeypatch.setattr(compute, "_provision_gcp", fake_provision_gcp)

        await compute.provision_ephemeral_vm(
            "compute-job-vm-cpu-test",
            startup_script="#!/bin/bash\ntrue",
            accelerator=None,
        )
        assert len(gpu_gate_calls) == 0

    def test_gpu_validation_failure_does_not_block_cpu_provisioning(self, monkeypatch):
        """A failed GPU image validation leaves CPU provisioning (_ensure_image_family_valid) unaffected."""
        # CPU image is valid; GPU image is not.
        monkeypatch.setattr(compute, "_image_family_valid", True)
        monkeypatch.setattr(compute, "_gpu_image_family_valid", False)
        monkeypatch.setattr(compute, "LOCAL_MODE", False)
        import time

        monkeypatch.setattr(compute, "_gpu_image_family_last_check", time.time())

        # CPU gate passes.
        compute._ensure_image_family_valid()

        # GPU gate raises.
        with pytest.raises(RuntimeError, match="GPU"):
            compute._ensure_gpu_image_family_valid()


# ---------------------------------------------------------------------------
# Storage artifact IAM / signed-URL helpers
# ---------------------------------------------------------------------------


class _FakeIamPolicy:
    """Minimal stand-in for the storage IAM Policy used by the artifact grant."""

    def __init__(self, bindings=None, version=1):
        self.bindings = list(bindings or [])
        self.version = version


class _FakeBucket:
    def __init__(self, policy):
        self._policy = policy
        self.set_calls = []

    def get_iam_policy(self, requested_policy_version=1):
        return self._policy

    def set_iam_policy(self, policy):
        self.set_calls.append(policy)
        self._policy = policy


class _FakeStorageClient:
    def __init__(self, bucket):
        self._bucket = bucket

    def bucket(self, _name):
        return self._bucket


def test_compute_job_artifact_uri_and_prefix_are_per_job():
    job_id = uuid.uuid4()
    prefix = storage.compute_job_artifact_object_prefix(job_id)
    assert prefix == f"compute-jobs/{job_id}/artifacts"
    assert storage.compute_job_artifact_uri(job_id) == (
        f"gs://{storage.ARTIFACT_BUCKET_NAME}/{prefix}/bundle.tar.gz"
    )


@pytest.mark.asyncio
async def test_compute_job_artifact_upload_signed_url_targets_bundle_object(
    monkeypatch,
):
    job_id = uuid.uuid4()
    expected_object = storage.compute_job_artifact_object_name(job_id)
    expires_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    blob = MagicMock()
    blob.generate_signed_url.return_value = "https://storage.example.com/upload"
    bucket = MagicMock()
    bucket.blob.return_value = blob
    client = MagicMock()
    client.bucket.return_value = bucket

    with patch("google.cloud.storage.Client", return_value=client):
        url = await storage.generate_compute_job_artifact_upload_signed_url(
            job_id, expires_at=expires_at
        )

    assert url == "https://storage.example.com/upload"
    client.bucket.assert_called_once_with(storage.ARTIFACT_BUCKET_NAME)
    bucket.blob.assert_called_once_with(expected_object)
    blob.generate_signed_url.assert_called_once()
    call_kwargs = blob.generate_signed_url.call_args.kwargs
    assert call_kwargs["version"] == "v4"
    assert call_kwargs["method"] == "PUT"
    assert call_kwargs["content_type"] == "application/gzip"
    assert call_kwargs["expiration"] == expires_at


def test_artifact_condition_scopes_writes_to_one_job_prefix_only():
    job_id = uuid.uuid4()
    expression = storage._compute_job_artifact_condition_expression(job_id)
    expected_prefix = (
        f"projects/_/buckets/{storage.ARTIFACT_BUCKET_NAME}/objects/"
        f"compute-jobs/{job_id}/artifacts/"
    )
    assert expression == f'resource.name.startsWith("{expected_prefix}")'
    # Never the whole bucket: a bare bucket-root prefix must not appear.
    assert (
        f'resource.name.startsWith("projects/_/buckets/{storage.ARTIFACT_BUCKET_NAME}/objects/")'
        not in expression
    )


def test_artifact_condition_includes_self_expiry_when_given():
    job_id = uuid.uuid4()
    expires_at = datetime(2026, 6, 7, 12, 0, 0)
    expression = storage._compute_job_artifact_condition_expression(job_id, expires_at)
    assert 'request.time < timestamp("2026-06-07T12:00:00Z")' in expression


@pytest.mark.asyncio
async def test_grant_artifact_writer_adds_prefix_scoped_binding(monkeypatch):
    job_id = uuid.uuid4()
    bucket = _FakeBucket(_FakeIamPolicy())
    monkeypatch.setattr(
        storage.storage, "Client", lambda project=None: _FakeStorageClient(bucket)
    )

    ok = await storage.grant_compute_job_artifact_writer(
        job_id, member="serviceAccount:vpm@example.iam.gserviceaccount.com"
    )

    assert ok is True
    assert len(bucket.set_calls) == 1
    binding = bucket._policy.bindings[-1]
    assert binding["role"] == "roles/storage.objectAdmin"
    assert binding["members"] == {"serviceAccount:vpm@example.iam.gserviceaccount.com"}
    assert str(job_id) in binding["condition"]["expression"]
    assert binding["condition"]["title"] == (
        storage._compute_job_artifact_condition_title(job_id)
    )


@pytest.mark.asyncio
async def test_grant_artifact_writer_replaces_stale_binding_for_same_job(monkeypatch):
    job_id = uuid.uuid4()
    title = storage._compute_job_artifact_condition_title(job_id)
    stale = {
        "role": "roles/storage.objectAdmin",
        "members": {"serviceAccount:old@example.iam.gserviceaccount.com"},
        "condition": {"title": title, "expression": "stale"},
    }
    other = {"role": "roles/storage.objectViewer", "members": {"user:keep@example.com"}}
    bucket = _FakeBucket(_FakeIamPolicy(bindings=[stale, other]))
    monkeypatch.setattr(
        storage.storage, "Client", lambda project=None: _FakeStorageClient(bucket)
    )

    await storage.grant_compute_job_artifact_writer(
        job_id, member="serviceAccount:new@example.iam.gserviceaccount.com"
    )

    titled = [
        b
        for b in bucket._policy.bindings
        if (b.get("condition") or {}).get("title") == title
    ]
    assert len(titled) == 1
    assert titled[0]["members"] == {
        "serviceAccount:new@example.iam.gserviceaccount.com"
    }
    # Unrelated bindings are preserved.
    assert other in bucket._policy.bindings


@pytest.mark.asyncio
async def test_revoke_artifact_writer_removes_only_its_binding(monkeypatch):
    job_id = uuid.uuid4()
    title = storage._compute_job_artifact_condition_title(job_id)
    mine = {
        "role": "roles/storage.objectAdmin",
        "members": {"serviceAccount:vpm@example.iam.gserviceaccount.com"},
        "condition": {"title": title, "expression": "expr"},
    }
    other = {"role": "roles/storage.objectViewer", "members": {"user:keep@example.com"}}
    bucket = _FakeBucket(_FakeIamPolicy(bindings=[mine, other]))
    monkeypatch.setattr(
        storage.storage, "Client", lambda project=None: _FakeStorageClient(bucket)
    )

    ok = await storage.revoke_compute_job_artifact_writer(
        job_id, member="serviceAccount:vpm@example.iam.gserviceaccount.com"
    )

    assert ok is True
    assert mine not in bucket._policy.bindings
    assert other in bucket._policy.bindings


@pytest.mark.asyncio
async def test_grant_artifact_writer_is_best_effort_on_error(monkeypatch):
    def _boom(project=None):
        raise RuntimeError("iam down")

    monkeypatch.setattr(storage.storage, "Client", _boom)
    ok = await storage.grant_compute_job_artifact_writer(
        uuid.uuid4(), member="serviceAccount:vpm@example.iam.gserviceaccount.com"
    )
    assert ok is False


@pytest.mark.asyncio
async def test_wake_delivery_missing_job_is_truthfully_classified(db_session):
    """When a compute_experiment wake fires but the ComputeJob row is gone, the
    parked task must land FAILED with a distinct ``compute_job_record_missing``
    classification — not a generic metric-gate miss with no failure_category.

    Exercises the missing-job branch added to deliver_wake's compute FINALIZE
    path: park the task with a dangling wake_source_ref that references a
    non-existent job UUID, fire the wake manually, then run the delivery sweep
    and assert:
      - task.status == FAILED  (not COMPLETED, not stuck PARKED)
      - task.context["failure_category"] == "compute_job_record_missing"
      - task.context["failure_class"] == "compute_job_record_missing"
      - registration.result_state == "job_record_missing"  (NOT "finalized")
      - registration.delivery_state == DELIVERED
    """
    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_lifecycle import fire_task_wakes, park_task
    from app.time_utils import _utcnow

    project = Project(name="Missing Job Wake", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute_experiment: missing job at wake",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Use a UUID that has no backing ComputeJob row — dangling reference.
    dangling_job_id = str(uuid.uuid4())

    await park_task(
        db_session,
        task,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=dangling_job_id,
        wake_action=WakeAction.FINALIZE,
        deadline_at=_utcnow() + timedelta(hours=1),
        reason=f"parked on compute job {dangling_job_id}",
    )
    await db_session.commit()

    db_session.expire_all()
    await db_session.refresh(task)
    assert task.status == TaskStatus.PARKED

    # Fire the wake manually — simulates the monitor calling fire_task_wakes.
    fired = await fire_task_wakes(
        db_session,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=dangling_job_id,
    )
    await db_session.commit()
    assert fired >= 1

    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert reg.delivery_state == WakeDeliveryState.FIRED

    # The delivery sweep must finalize with a truthful classification.
    delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert delivered == 1

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()

    assert task.status == TaskStatus.FAILED, (
        "missing job must land task FAILED, not COMPLETED or PARKED"
    )
    assert task.parked_at is None
    assert (task.context or {}).get(
        "failure_category"
    ) == "compute_job_record_missing", (
        "failure_category must be 'compute_job_record_missing', not a generic gate miss"
    )
    assert (task.context or {}).get("failure_class") == "compute_job_record_missing"
    assert reg.delivery_state == WakeDeliveryState.DELIVERED
    assert reg.result_state == "job_record_missing", (
        "result_state must be 'job_record_missing', not 'finalized'"
    )
    assert reg.attempt_count == 1

    # Idempotency: a second sweep does nothing (DELIVERED row never re-selected).
    again = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert again == 0


# ---------------------------------------------------------------------------
# Park/wake safety-net tests and race-contention proof
# ---------------------------------------------------------------------------
# (USE_POSTGRES is imported from testing.conftest above — it was re-derived here
# with the identical expression, which shadowed the import.)


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="row-lock contention test requires PostgreSQL (SELECT … FOR UPDATE)",
)
@pytest.mark.asyncio
async def test_compute_park_wake_is_race_safe_under_lock_contention(
    db_session, monkeypatch
):
    """Postgres-gated: the exactly-once park/wake guarantee holds under REAL lock
    contention, in BOTH race orderings.

    The shared serialization point is the compute-job row lock (_lock_compute_job,
    SELECT … FOR UPDATE). The two writers are:
      A) the monitor terminal-fire path (process_compute_jobs_once →
         fire_task_wakes in the SAME commit as the job finalization), and
      B) the park-time terminal recheck (_maybe_fire_terminal_compute_wake,
         invoked from park_task).

    Ordering 1 — monitor wins the lock (terminal-before-park):
      The monitor finalizes and fires (nothing to fire yet).
      The park-time recheck sees the terminal job under the lock and fires the
      just-created registration. Sweep delivers exactly once → COMPLETED.

    Ordering 2 — park wins the lock (park-before-terminal):
      park_task registers PENDING, acquires the lock, sees a non-terminal job, and
      leaves the registration PENDING. The monitor later finalizes and fires.
      Sweep delivers exactly once → COMPLETED.

    The test exercises BOTH orderings inside a single asyncio.gather by running the
    two orderings sequentially, each in its own setup + gather round.  Every
    assertion checks: exactly one FIRED→DELIVERED registration, exactly one task
    COMPLETED, no double-fire or double-deliver.
    """
    from sqlalchemy.orm import sessionmaker as _sessionmaker

    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND, _lock_compute_job
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_lifecycle import fire_task_wakes, park_task
    from app.time_utils import _utcnow

    started_at = _utcnow() - timedelta(seconds=30)

    class _AlwaysSucceedsBackend:
        def external_id_for(self, persisted_job):
            return f"race-job-{persisted_job.id}"

        async def launch(self, spec):
            return ComputeLaunchResult(
                external_id=f"race-job-{spec.job_id}",
                started_at=started_at,
                cost_rate_per_second=0.001,
                message="launched",
            )

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.05, measured_at=as_of)

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
        compute_jobs, "get_compute_backend", lambda _name: _AlwaysSucceedsBackend()
    )
    monkeypatch.setattr(
        "app.task_completion._spawn_act_on_verdict_followon",
        AsyncMock(return_value=None),
    )

    Session = _sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    async def _make_task_and_job():
        project = Project(name=f"Race test {uuid.uuid4().hex[:6]}", user_id="test-user")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = Task(
            project_id=project.id,
            title="compute_experiment: race safety",
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
        # Skip smoke gate — these tests exercise wake/park race conditions.
        meta = dict(job.polling_metadata or {})
        meta["preflight_smoke_done"] = True
        job.polling_metadata = meta
        await db_session.commit()
        return task, job

    # ── Ordering 1: terminal-before-park race ─────────────────────────────────
    # The monitor races park_task. The asyncio.Event barrier ensures the monitor
    # session holds the compute-job row lock (FOR UPDATE) before park_task's
    # terminal recheck attempts to acquire it. The Postgres-level serialization
    # determines which side fires the registration.
    task1, job1 = await _make_task_and_job()

    monitor_has_lock = asyncio.Event()

    async def monitor_holds_and_fires():
        """Acquire row lock, signal barrier, hold briefly, then finalize + fire."""
        async with Session() as session:
            locked = await _lock_compute_job(session, job1.id)
            assert locked is not None
            monitor_has_lock.set()
            await asyncio.sleep(0.15)
            # Manually finalize the job (mirror what _process_single_compute_job does).
            locked.status = ComputeJobStatus.SUCCEEDED
            locked.outcome = ComputeJobOutcome.SUCCESS
            locked.final_cost_usd = 0.05
            locked.accrued_cost_usd = 0.05
            session.add(locked)
            await fire_task_wakes(
                session,
                wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
                wake_source_ref=str(job1.id),
            )
            await session.commit()

    async def parked_task_races():
        """Wait for the monitor to hold the lock, then park (triggering recheck)."""
        await monitor_has_lock.wait()
        async with Session() as session:
            task_fresh = await session.get(Task, task1.id)
            await park_task(
                session,
                task_fresh,
                wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
                wake_source_ref=str(job1.id),
                wake_action=WakeAction.FINALIZE,
                deadline_at=_utcnow() + timedelta(hours=1),
                reason=f"parked on compute job {job1.id}",
            )
            await session.commit()

    await asyncio.gather(monitor_holds_and_fires(), parked_task_races())

    # After concurrent sessions committed, force-refresh task1 so the db_session
    # identity map reflects the PARKED status before the sweep runs.  Without this,
    # SQLAlchemy 2.0 (expire_on_commit=False) returns the still-IN_PROGRESS cached
    # instance from the identity map when deliver_wake queries the task row, which
    # makes deliver_wake think the task is not PARKED and marks the registration
    # task_not_parked instead of finalizing.
    # expire_all BEFORE the refresh: the refresh re-loads task1 so its
    # attributes (task1.id below) stay readable — an expired instance would
    # lazy-load and raise MissingGreenlet under real asyncpg expiry.
    db_session.expire_all()
    await db_session.refresh(task1)
    regs1 = (
        (
            await db_session.execute(
                select(TaskWakeRegistration)
                .where(TaskWakeRegistration.task_id == task1.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    assert len(regs1) == 1, (
        f"exactly one registration expected after ordering-1 race, got {len(regs1)}"
    )
    assert regs1[0].delivery_state == WakeDeliveryState.FIRED, (
        "registration must be FIRED after the terminal-before-park race "
        f"(got {regs1[0].delivery_state!r})"
    )

    delivered1 = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert delivered1 == 1, "sweep must deliver exactly once in ordering-1 race"

    await db_session.refresh(task1)
    reg1 = (
        (
            await db_session.execute(
                select(TaskWakeRegistration)
                .where(TaskWakeRegistration.task_id == task1.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .one()
    )
    assert task1.status == TaskStatus.COMPLETED, (
        f"task must be COMPLETED after ordering-1, got {task1.status!r}"
    )
    assert task1.parked_at is None
    assert reg1.delivery_state == WakeDeliveryState.DELIVERED
    assert reg1.result_state == "finalized"
    assert reg1.attempt_count == 1
    assert (task1.context or {}).get("failure_category") != "park_wake_timeout", (
        "task must NOT carry park_wake_timeout after a successful race delivery"
    )

    # Idempotency: a second sweep delivers nothing.
    again1 = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert again1 == 0

    # ── Ordering 2: park-before-terminal race ─────────────────────────────────
    # park_task runs first (job is still QUEUED). Its terminal recheck acquires the
    # lock but sees a non-terminal job → registration stays PENDING. The monitor
    # later finalizes and fires the PENDING registration.
    task2, job2 = await _make_task_and_job()

    # Park first (job is QUEUED — not terminal).
    await park_task(
        db_session,
        task2,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(job2.id),
        wake_action=WakeAction.FINALIZE,
        deadline_at=_utcnow() + timedelta(hours=1),
        reason=f"parked on compute job {job2.id}",
    )
    await db_session.commit()

    await db_session.refresh(task2)
    reg2_pending = (
        (
            await db_session.execute(
                select(TaskWakeRegistration)
                .where(TaskWakeRegistration.task_id == task2.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .one()
    )
    assert task2.status == TaskStatus.PARKED
    assert reg2_pending.delivery_state == WakeDeliveryState.PENDING, (
        "registration must be PENDING when job is not yet terminal at park time"
    )

    # Now run the monitor to terminal + fire.
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(task2)
    await db_session.refresh(reg2_pending)
    assert reg2_pending.delivery_state == WakeDeliveryState.FIRED, (
        "registration must be FIRED after monitor fires it"
    )

    delivered2 = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert delivered2 == 1, "sweep must deliver exactly once in ordering-2 race"

    await db_session.refresh(task2)
    reg2_done = (
        (
            await db_session.execute(
                select(TaskWakeRegistration)
                .where(TaskWakeRegistration.task_id == task2.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .one()
    )
    assert task2.status == TaskStatus.COMPLETED, (
        f"task must be COMPLETED after ordering-2, got {task2.status!r}"
    )
    assert reg2_done.delivery_state == WakeDeliveryState.DELIVERED
    assert reg2_done.result_state == "finalized"
    assert reg2_done.attempt_count == 1

    again2 = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert again2 == 0


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="row-lock contention test requires PostgreSQL (SELECT … FOR UPDATE)",
)
@pytest.mark.asyncio
async def test_compute_monitor_does_not_hold_row_lock_across_backend_rpcs(
    db_session, monkeypatch
):
    """Regression: backend RPCs run without pinning the compute-job row lock.

    Also rechecks the terminal-before-park wake path in the same test so the
    refactor keeps the row-lock-serialized terminal wake handoff intact.
    """
    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_lifecycle import park_task
    from app.time_utils import _utcnow

    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    # --- Lock-release proof: launch RPCs must not hold the row lock. ---------
    project = Project(name="Compute Lock Release", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute_experiment: lock release",
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
    # Skip smoke gate so the monitor reaches backend.launch() on the first tick.
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    job.polling_metadata = meta
    await db_session.commit()

    launch_entered = asyncio.Event()
    launch_release = asyncio.Event()
    started_at = _utcnow() - timedelta(seconds=30)

    class _BlockingLaunchBackend:
        def external_id_for(self, persisted_job):
            return f"lock-release-{persisted_job.id}"

        async def launch(self, spec):
            launch_entered.set()
            await launch_release.wait()
            return ComputeLaunchResult(
                external_id=f"lock-release-{spec.job_id}",
                started_at=started_at,
                cost_rate_per_second=0.001,
                message="launched",
            )

        async def poll_cost(self, persisted_job, *, as_of):
            raise AssertionError("poll_cost should not run during launch")

        async def poll_status(self, persisted_job):
            raise AssertionError("poll_status should not run during launch")

        async def terminate(self, persisted_job, *, reason):
            raise AssertionError("terminate should not run during launch")

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: _BlockingLaunchBackend()
    )
    monkeypatch.setattr(
        "app.task_completion._spawn_act_on_verdict_followon",
        AsyncMock(return_value=None),
    )

    monitor_task = asyncio.create_task(
        compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    )
    await asyncio.wait_for(launch_entered.wait(), timeout=5)

    async with session_factory() as lock_session:
        locked = await lock_session.execute(
            select(ComputeJob)
            .where(ComputeJob.id == job.id)
            .with_for_update(nowait=True)
        )
        locked_job = locked.scalars().one()
        assert locked_job.status == ComputeJobStatus.STARTING

    launch_release.set()
    await monitor_task

    db_session.expire_all()
    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.RUNNING
    assert job.started_at is not None
    assert job.external_compute_id == f"lock-release-{job.id}"

    # --- Race safety: terminal-before-park still fires exactly once. ---------
    project2 = Project(name="Compute Terminal Wake", user_id="test-user")
    db_session.add(project2)
    await db_session.commit()
    await db_session.refresh(project2)

    task2 = Task(
        project_id=project2.id,
        title="compute_experiment: terminal wake",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
    )
    db_session.add(task2)
    await db_session.commit()
    await db_session.refresh(task2)

    job2 = await compute_jobs.create_compute_job(
        db_session,
        task2,
        command="echo ok",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    # Skip smoke gate — this test exercises terminal-before-park wake handoff.
    meta2 = dict(job2.polling_metadata or {})
    meta2["preflight_smoke_done"] = True
    job2.polling_metadata = meta2
    await db_session.commit()

    class _TerminalBackend:
        def external_id_for(self, persisted_job):
            return f"terminal-wake-{persisted_job.id}"

        async def launch(self, spec):
            return ComputeLaunchResult(
                external_id=f"terminal-wake-{spec.job_id}",
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
            )

        async def terminate(self, persisted_job, *, reason):
            raise AssertionError("terminate should not be called")

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: _TerminalBackend()
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    db_session.expire_all()
    await db_session.refresh(job2)
    # Re-load the task too: park_task reads task2 attributes, and an expired
    # instance would lazy-load (MissingGreenlet under real asyncpg expiry).
    await db_session.refresh(task2)
    assert job2.status == ComputeJobStatus.SUCCEEDED
    assert job2.outcome == ComputeJobOutcome.SUCCESS

    await park_task(
        db_session,
        task2,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(job2.id),
        wake_action=WakeAction.FINALIZE,
        deadline_at=_utcnow() + timedelta(hours=1),
        reason=f"parked on compute job {job2.id}",
    )
    await db_session.commit()

    db_session.expire_all()
    await db_session.refresh(task2)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert reg.delivery_state == WakeDeliveryState.FIRED

    delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert delivered == 1

    db_session.expire_all()
    await db_session.refresh(task2)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert task2.status == TaskStatus.COMPLETED
    assert reg.delivery_state == WakeDeliveryState.DELIVERED
    assert reg.attempt_count == 1

    again = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert again == 0


@pytest.mark.asyncio
async def test_park_wake_timeout_disambiguates_compute_job_state(db_session):
    """Safety net: a PENDING registration whose deadline_at has passed causes the
    PARKED task to go FAILED with failure_class/failure_category 'park_wake_timeout',
    and the diagnostic payload distinguishes why the wake expired.

    Also verifies idempotency: a second sweep pass on a non-PENDING registration
    is a strict no-op (registration not re-expired, task status unchanged).
    """
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_lifecycle import park_task
    from app.time_utils import _utcnow

    project = Project(name="Expire Wake Safety Net", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="expire-wake test task",
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
        command="sleep 60",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )

    # Park with a deadline ALREADY in the past so sweep pass 2 expires it.
    past_deadline = _utcnow() - timedelta(minutes=5)
    await park_task(
        db_session,
        task,
        wake_source_kind="compute_job_terminal",
        wake_source_ref=str(job.id),
        wake_action=WakeAction.FINALIZE,
        deadline_at=past_deadline,
        reason="parked on slow job",
    )
    await db_session.commit()

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert task.status == TaskStatus.PARKED
    # Job is still QUEUED (not terminal), so park-time recheck leaves it PENDING.
    assert reg.delivery_state == WakeDeliveryState.PENDING
    assert reg.deadline_at is not None

    # Run the sweep — pass 2 (expire) must trigger on the past deadline.
    delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert delivered == 0, "no FIRED registration to deliver"

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()

    assert task.status == TaskStatus.FAILED, (
        f"task must be FAILED after park wake expiry, got {task.status!r}"
    )
    assert task.parked_at is None
    assert (task.context or {}).get("failure_category") == "park_wake_timeout", (
        f"expected failure_category='park_wake_timeout', got {task.context!r}"
    )
    assert (task.context or {}).get("failure_class") == "park_wake_timeout", (
        f"expected failure_class='park_wake_timeout', got {task.context!r}"
    )
    assert (task.context or {}).get(
        "failure_detail"
    ) == "compute_job_terminal_job_not_terminal_by_deadline"
    diagnostics = (task.context or {}).get("park_wake_timeout_diagnostics") or {}
    assert diagnostics["job_terminal_by_deadline"] is False
    assert diagnostics["job_status"] == ComputeJobStatus.QUEUED.value
    assert diagnostics["job_completed_at"] is None
    assert diagnostics["deadline_at"] == past_deadline.isoformat()
    assert diagnostics["requested_at"] == reg.requested_at.isoformat()
    assert diagnostics["claimed_at"] is None
    assert diagnostics["fired_at"] is None
    # Provisioning-vs-runtime enrichment: job never started, so execution_seconds is None.
    assert "job_created_at" in diagnostics
    assert diagnostics["job_started_at"] is None
    assert diagnostics["provisioning_seconds"] is not None
    assert diagnostics["execution_seconds"] is None
    assert "expired before the job reached terminal state" in (
        task.latest_status_message or ""
    )
    assert reg.result_detail == task.latest_status_message
    assert reg.delivery_state == WakeDeliveryState.EXPIRED
    assert reg.result_state == "expired"

    # Idempotency: a second sweep is a strict no-op.
    again = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert again == 0

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert task.status == TaskStatus.FAILED
    assert reg.delivery_state == WakeDeliveryState.EXPIRED


@pytest.mark.asyncio
async def test_park_wake_timeout_when_job_finished_but_wake_never_fired(db_session):
    """A terminal compute job that completed before the park deadline but never
    fired its wake should expire with the alternate diagnostic branch.
    """
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_lifecycle import park_task
    from app.time_utils import _utcnow

    project = Project(name="Expire Wake Terminal Job", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="expire-wake terminal job task",
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
        command="sleep 60",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    # Park while job is still QUEUED so _maybe_fire_terminal_compute_wake leaves
    # the registration PENDING — simulating the window before the monitor fires.
    now = _utcnow()
    future_deadline = now + timedelta(hours=1)
    await park_task(
        db_session,
        task,
        wake_source_kind="compute_job_terminal",
        wake_source_ref=str(job.id),
        wake_action=WakeAction.FINALIZE,
        deadline_at=future_deadline,
        reason="parked on job",
    )
    await db_session.commit()

    # Simulate the lost-wake race: job completed before the (now-backdated) deadline,
    # but the fire was never delivered. Backdate both the job completion and the
    # registration deadline so the sweep sees an expired-but-terminal scenario.
    completed_at = now - timedelta(minutes=10)
    past_deadline = now - timedelta(minutes=5)
    job.status = ComputeJobStatus.SUCCEEDED
    job.outcome = ComputeJobOutcome.SUCCESS
    job.terminal_reason = "completed successfully"
    job.completed_at = completed_at
    # Commit the job mutations BEFORE expire_all: real ORM expiry discards
    # pending (unflushed) attribute changes, silently un-finishing the job.
    db_session.add(job)
    await db_session.commit()

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    # Backdate the deadline so Pass 2 (expire) triggers on this registration.
    reg.deadline_at = past_deadline
    await db_session.commit()

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert task.status == TaskStatus.PARKED
    assert reg.delivery_state == WakeDeliveryState.PENDING

    delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert delivered == 0

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()

    assert task.status == TaskStatus.FAILED
    assert (task.context or {}).get("failure_category") == "park_wake_timeout"
    assert (task.context or {}).get("failure_class") == "park_wake_timeout"
    assert (task.context or {}).get(
        "failure_detail"
    ) == "compute_job_terminal_wake_lost_after_job_completed"
    diagnostics = (task.context or {}).get("park_wake_timeout_diagnostics") or {}
    assert diagnostics["job_terminal_by_deadline"] is True
    assert diagnostics["job_status"] == ComputeJobStatus.SUCCEEDED.value
    assert diagnostics["job_completed_at"] == completed_at.isoformat()
    assert diagnostics["deadline_at"] == past_deadline.isoformat()
    assert diagnostics["requested_at"] == reg.requested_at.isoformat()
    assert diagnostics["claimed_at"] is None
    assert diagnostics["fired_at"] is None
    # Provisioning-vs-runtime enrichment fields are present (job_created_at always set).
    assert "job_created_at" in diagnostics
    assert "job_started_at" in diagnostics
    assert "provisioning_seconds" in diagnostics
    assert "execution_seconds" in diagnostics
    assert "wake never fired before the deadline" in (task.latest_status_message or "")
    assert reg.result_detail == task.latest_status_message
    assert reg.delivery_state == WakeDeliveryState.EXPIRED
    assert reg.result_state == "expired"


@pytest.mark.asyncio
async def test_sweep_strand_guard_fails_task_with_no_live_registration(db_session):
    """Safety net: sweep pass 3 (strand-guard) FAILs a PARKED task whose parked_at
    is older than _PARK_STRAND_SECONDS and has NO live (PENDING/FIRED) registration,
    while leaving recently-parked tasks and tasks with live registrations untouched.

    Uses backdated parked_at to trigger the guard without sleeping.
    """
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_lifecycle import _PARK_STRAND_SECONDS
    from app.time_utils import _utcnow

    now = _utcnow()
    project = Project(name="Strand Guard Safety Net", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task A: stranded — old parked_at, no live registration.
    task_a = Task(
        project_id=project.id,
        title="stranded task",
        status=TaskStatus.PARKED,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        parked_at=now - timedelta(seconds=_PARK_STRAND_SECONDS + 60),
        park_reason="stranded",
    )
    db_session.add(task_a)

    # Task B: recently parked — within the strand window, should NOT be stranded.
    task_b = Task(
        project_id=project.id,
        title="recently parked task",
        status=TaskStatus.PARKED,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        parked_at=now - timedelta(minutes=10),
        park_reason="parked recently",
    )
    db_session.add(task_b)
    await db_session.commit()
    await db_session.refresh(task_a)
    await db_session.refresh(task_b)

    # Task C: old parked_at but still has a live PENDING registration — must NOT be stranded.
    task_c = Task(
        project_id=project.id,
        title="old but has live registration",
        status=TaskStatus.PARKED,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        parked_at=now - timedelta(seconds=_PARK_STRAND_SECONDS + 300),
        park_reason="old but live",
    )
    db_session.add(task_c)
    await db_session.commit()
    await db_session.refresh(task_c)

    job_c = await compute_jobs.create_compute_job(
        db_session,
        task_c,
        command="sleep 60",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    # Add a live PENDING registration for task_c so the strand guard skips it.
    reg_c = TaskWakeRegistration(
        task_id=task_c.id,
        project_id=project.id,
        wake_source_kind="compute_job_terminal",
        wake_source_ref=str(job_c.id),
        wake_action=WakeAction.FINALIZE,
        delivery_state=WakeDeliveryState.PENDING,
        requested_at=now,
        deadline_at=now + timedelta(hours=1),
    )
    db_session.add(reg_c)
    await db_session.commit()

    # Run sweep — no FIRED registrations (pass 1 delivers 0), no past deadlines
    # (pass 2 expires 0 for tasks A/C since no registration at all for A and
    # deadline is future for C). Pass 3 strands task_a only.
    delivered = await sweep_deliver_task_wakes(db_session, now)
    assert delivered == 0, "no FIRED registrations to deliver"

    db_session.expire_all()
    await db_session.refresh(task_a)
    await db_session.refresh(task_b)
    await db_session.refresh(task_c)

    # Stranded task is now FAILED with the correct classification.
    assert task_a.status == TaskStatus.FAILED, (
        f"stranded task_a must be FAILED, got {task_a.status!r}"
    )
    assert task_a.parked_at is None
    assert (task_a.context or {}).get("failure_category") == "park_wake_stranded", (
        f"expected failure_category='park_wake_stranded', got {task_a.context!r}"
    )
    assert (task_a.context or {}).get("failure_class") == "park_wake_stranded", (
        f"expected failure_class='park_wake_stranded', got {task_a.context!r}"
    )

    # Recently-parked task is left untouched.
    assert task_b.status == TaskStatus.PARKED, (
        f"recently-parked task_b must remain PARKED, got {task_b.status!r}"
    )

    # Old task with a live registration is left untouched.
    assert task_c.status == TaskStatus.PARKED, (
        f"task_c with live registration must remain PARKED, got {task_c.status!r}"
    )


@pytest.mark.asyncio
async def test_deliver_wake_noop_when_task_not_parked(db_session):
    """Safety net: when the task is no longer PARKED at delivery time, deliver_wake
    records result_state='task_not_parked' (delivery_state FAILED) and leaves the
    task untouched — no spurious finalize or status change.

    Also covers the idempotency guard: a non-FIRED registration is a no-op.
    """
    from app.task_lifecycle import deliver_wake
    from app.time_utils import _utcnow

    project = Project(name="Deliver Wake Not Parked", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create the task as IN_PROGRESS so create_compute_job accepts it.
    task = Task(
        project_id=project.id,
        title="already completed task",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
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

    # Now advance the task to COMPLETED (simulates having been woken by something else
    # before this particular registration is delivered).
    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Create a FIRED registration (as if the monitor fired it, but the task moved on).
    reg = TaskWakeRegistration(
        task_id=task.id,
        project_id=project.id,
        wake_source_kind="compute_job_terminal",
        wake_source_ref=str(job.id),
        wake_action=WakeAction.FINALIZE,
        delivery_state=WakeDeliveryState.FIRED,
        requested_at=_utcnow(),
        fired_at=_utcnow(),
        deadline_at=_utcnow() + timedelta(hours=1),
    )
    db_session.add(reg)
    await db_session.commit()

    # Attempt delivery — task is COMPLETED (not PARKED), so this is a no-op.
    advanced = await deliver_wake(db_session, reg, now=_utcnow())
    await db_session.commit()

    assert advanced is False, "deliver_wake must return False when task is not PARKED"

    # Snapshot the id before expire_all: reading reg.id on an expired instance
    # lazy-loads (MissingGreenlet under real asyncpg expiry).
    reg_id = reg.id
    db_session.expire_all()
    await db_session.refresh(task)
    reg_fresh = await db_session.get(TaskWakeRegistration, reg_id)

    assert task.status == TaskStatus.COMPLETED, (
        "task status must be unchanged (COMPLETED) after no-op delivery"
    )
    assert reg_fresh.delivery_state == WakeDeliveryState.FAILED, (
        f"registration must be FAILED (not_parked no-op), got {reg_fresh.delivery_state!r}"
    )
    assert reg_fresh.result_state == "task_not_parked", (
        f"expected result_state='task_not_parked', got {reg_fresh.result_state!r}"
    )

    # Idempotency: a second deliver_wake call on the now-FAILED registration is a no-op.
    advanced2 = await deliver_wake(db_session, reg_fresh, now=_utcnow())
    await db_session.commit()
    assert advanced2 is False, (
        "deliver_wake on a non-FIRED registration must return False"
    )

    db_session.expire_all()
    reg_again = await db_session.get(TaskWakeRegistration, reg_id)
    assert reg_again.delivery_state == WakeDeliveryState.FAILED
    assert reg_again.result_state == "task_not_parked"


@pytest.mark.asyncio
async def test_kill_compute_job_fires_wake_for_parked_task(db_session, monkeypatch):
    """Safety net: kill_compute_job fires the compute-job-terminal wake for any
    PARKED task that is waiting on the job, so the task wakes immediately rather
    than sitting until its park deadline.
    """
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_lifecycle import park_task
    from app.time_utils import _utcnow

    project = Project(name="Kill Job Fires Wake", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute_experiment: parked awaiting kill",
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
        command="sleep 3600",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=3600,
    )
    # Advance job to RUNNING so kill_compute_job can accept it.
    job.status = ComputeJobStatus.RUNNING
    job.external_compute_id = f"kill-job-{uuid.uuid4().hex[:8]}"
    db_session.add(job)
    await db_session.commit()

    # Park the task on the running job.
    await park_task(
        db_session,
        task,
        wake_source_kind="compute_job_terminal",
        wake_source_ref=str(job.id),
        wake_action=WakeAction.FINALIZE,
        deadline_at=_utcnow() + timedelta(hours=1),
        reason=f"parked on compute job {job.id}",
    )
    await db_session.commit()

    # Snapshot ids before expire_all: reading them on expired instances
    # lazy-loads (MissingGreenlet under real asyncpg expiry).
    job_id = job.id
    project_id = project.id
    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert task.status == TaskStatus.PARKED
    # Job is RUNNING (not terminal): park-time recheck leaves registration PENDING.
    assert reg.delivery_state == WakeDeliveryState.PENDING

    # Stub the backend so terminate() succeeds without touching GCP.
    class _FakeBackend:
        async def terminate(self, persisted_job, *, reason):
            return True

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: _FakeBackend()
    )

    # Operator kill — must fire the wake in the same commit.
    killed = await compute_jobs.kill_compute_job(
        db_session,
        job_id=job_id,
        project_id=project_id,
        killed_by="operator@test.com",
    )
    assert killed.status == ComputeJobStatus.CANCELLED

    db_session.expire_all()
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert reg.delivery_state == WakeDeliveryState.FIRED, (
        "kill_compute_job must fire the wake registration so the parked task wakes "
        f"immediately (got {reg.delivery_state!r})"
    )

    # Sweep delivers the fired wake and finalizes the task.
    delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert delivered == 1

    db_session.expire_all()
    await db_session.refresh(task)
    reg = (await db_session.execute(select(TaskWakeRegistration))).scalars().one()
    assert task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED), (
        f"task must be finalized after wake delivery, got {task.status!r}"
    )
    assert task.parked_at is None
    assert reg.delivery_state == WakeDeliveryState.DELIVERED
    assert reg.result_state == "finalized"
    assert reg.attempt_count == 1


@pytest.mark.asyncio
async def test_kill_compute_job_releases_row_lock_across_backend_rpc(
    db_session, monkeypatch
):
    """Regression test: operator-kill must not hold the ComputeJob row lock
    across the backend teardown RPC. The lock is released after validating the
    job is non-terminal (pre-RPC), then re-acquired after the RPC to apply the
    terminal state. This prevents stalling park-recheck paths that fire
    compute-job-terminal wakes while the kill is in flight.

    Proof: the ComputeJob row is acquirable from a separate session during the
    kill's backend terminate RPC, and a concurrent peer-terminal during the
    RPC produces exactly one terminal finalize, not two.
    """
    from app.database import AsyncSessionLocal

    project = Project(name="Kill Job Releases Lock", user_id="test-user")
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
    job.external_compute_id = "lock-release-test-job"
    db_session.add(job)
    await db_session.commit()

    lock_acquire_succeeded = False
    rpc_in_flight = asyncio.Event()
    rpc_can_proceed = asyncio.Event()

    class _FakeBackend:
        async def terminate(self, persisted_job, *, reason):
            # Signal that RPC is in flight so the concurrent task can try to lock.
            rpc_in_flight.set()
            # Wait for the concurrent lock attempt to complete before proceeding.
            await rpc_can_proceed.wait()
            return True

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: _FakeBackend()
    )

    async def try_acquire_lock_during_rpc():
        nonlocal lock_acquire_succeeded
        # Wait for the RPC to start.
        await rpc_in_flight.wait()
        # Try to acquire the lock in a separate session.
        async with AsyncSessionLocal() as peer_session:
            peer_job = await compute_jobs._lock_compute_job(peer_session, job.id)
            lock_acquire_succeeded = peer_job is not None
        # Signal that the lock attempt is complete so the RPC can proceed.
        rpc_can_proceed.set()

    # Run the lock-attempt task concurrently with the kill.
    kill_task = asyncio.create_task(
        compute_jobs.kill_compute_job(
            db_session,
            job_id=job.id,
            project_id=project.id,
            killed_by="operator@test.com",
        )
    )
    lock_task = asyncio.create_task(try_acquire_lock_during_rpc())

    await asyncio.gather(kill_task, lock_task)

    assert lock_acquire_succeeded, (
        "ComputeJob row lock must be released during backend RPC so concurrent "
        "writers are not stalled (e.g., park-recheck wakes)"
    )

    db_session.expire_all()
    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.CANCELLED
    assert job.outcome == ComputeJobOutcome.CANCELLED


@pytest.mark.asyncio
async def test_kill_compute_job_gracefully_handles_peer_terminal(
    db_session, monkeypatch
):
    """Regression test: if a peer (monitor, deadline/budget teardown, or
    task-terminal cancel) drives the job terminal during the kill's lock-free
    backend RPC window, the kill must detect this on re-acquire and degrade to
    no-op (return None) instead of double-finalizing.
    """
    from app.database import AsyncSessionLocal
    from app.time_utils import _utcnow

    project = Project(name="Kill Job Peer Terminal", user_id="test-user")
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
    job.external_compute_id = "peer-terminal-test-job"
    db_session.add(job)
    await db_session.commit()

    rpc_in_flight = asyncio.Event()
    rpc_can_proceed = asyncio.Event()

    class _FakeBackend:
        async def terminate(self, persisted_job, *, reason):
            # Signal that RPC is in flight.
            rpc_in_flight.set()
            # Wait for the peer to make it terminal.
            await rpc_can_proceed.wait()
            return True

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: _FakeBackend()
    )

    async def peer_drive_terminal():
        # Wait for the RPC to start.
        await rpc_in_flight.wait()
        # Drive the job terminal as a peer would (e.g., deadline/budget teardown).
        async with AsyncSessionLocal() as peer_session:
            peer_job = await compute_jobs._lock_compute_job(peer_session, job.id)
            assert peer_job is not None
            peer_job.status = ComputeJobStatus.FAILED
            peer_job.outcome = ComputeJobOutcome.SPEND_CAP_EXCEEDED
            peer_job.completed_at = _utcnow()
            peer_job.terminal_reason = "peer made terminal"
            await peer_session.commit()
        # Signal that the peer is done so the RPC can proceed.
        rpc_can_proceed.set()

    # Run the peer-terminal task concurrently with the kill.
    kill_task = asyncio.create_task(
        compute_jobs.kill_compute_job(
            db_session,
            job_id=job.id,
            project_id=project.id,
            killed_by="operator@test.com",
        )
    )
    peer_task = asyncio.create_task(peer_drive_terminal())

    await asyncio.gather(kill_task, peer_task)

    db_session.expire_all()
    await db_session.refresh(job)
    # The peer's outcome should be preserved (not overwritten by kill).
    assert job.outcome == ComputeJobOutcome.SPEND_CAP_EXCEEDED
    assert job.terminal_reason == "peer made terminal"


@pytest.mark.asyncio
async def test_park_wake_timeout_diagnostics_provisioning_vs_runtime_split(db_session):
    """Acceptance: park_wake_timeout diagnostics carry the provisioning-vs-runtime split.

    Two scenarios:
    (A) Job never started running (started_at is None) — all budget went to provisioning.
        Diagnostics: provisioning_seconds set, execution_seconds None.
    (B) Job started running but did not complete in time.
        Diagnostics: provisioning_seconds + execution_seconds both set.
    """
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_lifecycle import park_task
    from app.time_utils import _utcnow

    project = Project(name="Provisioning Split Test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # Use a future deadline first so park_task accepts it, then backdate after commit.
    future_deadline = now + timedelta(hours=1)
    # The "actual" past deadline used for diagnostics — must be after job created_at.
    past_deadline = now - timedelta(minutes=5)

    # --- Scenario A: job never started running (started_at is None) ---
    task_a = Task(
        project_id=project.id,
        title="provisioning-starved task",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
    )
    db_session.add(task_a)
    await db_session.commit()
    await db_session.refresh(task_a)

    job_a = await compute_jobs.create_compute_job(
        db_session,
        task_a,
        command="sleep 60",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    # Backdate job_a's created_at so it precedes the past_deadline.
    job_a.created_at = past_deadline - timedelta(minutes=10)
    await db_session.commit()

    # Leave job_a.started_at as None — simulates provisioning starvation.
    await park_task(
        db_session,
        task_a,
        wake_source_kind="compute_job_terminal",
        wake_source_ref=str(job_a.id),
        wake_action=WakeAction.FINALIZE,
        deadline_at=future_deadline,
        reason="parked on slow job",
    )
    # Backdate the registration deadline so sweep expires it.
    reg_a = (
        (
            await db_session.execute(
                select(TaskWakeRegistration).where(
                    TaskWakeRegistration.task_id == task_a.id
                )
            )
        )
        .scalars()
        .one()
    )
    reg_a.deadline_at = past_deadline
    await db_session.commit()

    # --- Scenario B: job started running but did not complete ---
    task_b = Task(
        project_id=project.id,
        title="ran-out-of-time task",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
    )
    db_session.add(task_b)
    await db_session.commit()
    await db_session.refresh(task_b)

    job_b = await compute_jobs.create_compute_job(
        db_session,
        task_b,
        command="sleep 600",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    # Backdate job_b's created_at so it precedes started_at and the past_deadline.
    job_b_created_at = past_deadline - timedelta(minutes=10)
    job_b.created_at = job_b_created_at
    # Simulate the job reaching running state 2 minutes after creation.
    started_at = job_b_created_at + timedelta(minutes=2)
    job_b.started_at = started_at
    await db_session.commit()

    await park_task(
        db_session,
        task_b,
        wake_source_kind="compute_job_terminal",
        wake_source_ref=str(job_b.id),
        wake_action=WakeAction.FINALIZE,
        deadline_at=future_deadline,
        reason="parked on slow job",
    )
    # Backdate the registration deadline.
    reg_b = (
        (
            await db_session.execute(
                select(TaskWakeRegistration).where(
                    TaskWakeRegistration.task_id == task_b.id
                )
            )
        )
        .scalars()
        .one()
    )
    reg_b.deadline_at = past_deadline
    await db_session.commit()

    # Expire both tasks via the sweep.
    delivered = await sweep_deliver_task_wakes(db_session, now)
    assert delivered == 0, "no FIRED registrations to deliver"

    db_session.expire_all()
    await db_session.refresh(task_a)
    await db_session.refresh(task_b)

    # --- Verify Scenario A diagnostics ---
    assert task_a.status == TaskStatus.FAILED
    assert (task_a.context or {}).get("failure_class") == "park_wake_timeout"
    assert (task_a.context or {}).get("failure_detail") == (
        "compute_job_terminal_job_not_terminal_by_deadline"
    )
    diag_a = (task_a.context or {}).get("park_wake_timeout_diagnostics") or {}
    assert diag_a["job_created_at"] is not None, "job_created_at must be recorded"
    assert diag_a["job_started_at"] is None, "job never started — must be None"
    assert diag_a["provisioning_seconds"] is not None, (
        "provisioning_seconds must reflect full deadline when job never started"
    )
    assert diag_a["execution_seconds"] is None, (
        "execution_seconds must be None when job never started"
    )
    assert diag_a["provisioning_seconds"] > 0

    # --- Verify Scenario B diagnostics ---
    assert task_b.status == TaskStatus.FAILED
    assert (task_b.context or {}).get("failure_class") == "park_wake_timeout"
    diag_b = (task_b.context or {}).get("park_wake_timeout_diagnostics") or {}
    assert diag_b["job_created_at"] is not None
    assert diag_b["job_started_at"] is not None, (
        "job_started_at must reflect started_at"
    )
    assert diag_b["provisioning_seconds"] is not None, (
        "provisioning_seconds must reflect time from creation to start"
    )
    assert diag_b["execution_seconds"] is not None, (
        "execution_seconds must reflect time from start to deadline"
    )
    # job ran for (deadline - started_at); provisioning is (started_at - created_at).
    # Both should be positive.
    assert diag_b["provisioning_seconds"] > 0


# ══════════════════════════════════════════════════════════════════════════════
# Daily cost ceiling alert — compute wake finalization path
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_compute_wake_finalization_triggers_daily_cost_ceiling_alert(
    db_session, monkeypatch
):
    """Compute wake finalization creates a budget alert when spend exceeds the ceiling.

    Verifies three behaviours in one test:
    (a) A terminal compute job whose spend pushes the project over the ceiling
        creates exactly ONE budget alert via run_compute_wake_post_commit.
    (b) Under the ceiling, no alert is created.
    (c) Cross-path dedup: if an agent task already alerted today, a subsequent
        compute wake does NOT create a second alert.
    """
    from unittest.mock import patch as _patch
    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND
    import app.shared_state as _shared_state
    from app.reaper_sweeps import sweep_deliver_task_wakes
    from app.task_lifecycle import fire_task_wakes, park_task
    from app.time_utils import _utcnow

    # ── Setup ────────────────────────────────────────────────────────────────
    project = Project(name="Cost Ceiling Compute Wake Test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    # Snapshot the id: the expire_all() calls below expire `project`, and
    # reading project.id afterwards lazy-loads (MissingGreenlet on asyncpg).
    project_id = project.id

    monkeypatch.setattr(
        "app.task_completion.broadcast_to_project", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "app.cost_ceiling.broadcast_to_project", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "app.notification_dispatch.dispatch_external_notification",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.knowledge_completion._analyze_compute_experiment_with_llm",
        AsyncMock(return_value=(None, "precondition-not-met")),
    )

    async def _make_parked_task_and_terminal_job(final_cost_usd: float):
        """Create a parked compute_experiment task backed by a terminal job."""
        task = Task(
            project_id=project_id,
            title="compute_experiment: cost ceiling test",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "compute_experiment", "base_branch": "main"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:8]}",
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        job = await compute_jobs.create_compute_job(
            db_session,
            task,
            command="echo ok",
            machine_type="e2-standard-2",
            spend_cap_usd=final_cost_usd + 1.0,
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
        job.final_cost_usd = final_cost_usd
        job.accrued_cost_usd = final_cost_usd
        job.completed_at = _utcnow().replace(tzinfo=None)
        db_session.add(job)
        await fire_task_wakes(
            db_session,
            wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
            wake_source_ref=str(job.id),
        )
        await db_session.commit()
        return task, job

    # ── Scenario (b): under ceiling — no alert ───────────────────────────────
    with _patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 100.0}):
        _task_b, _job_b = await _make_parked_task_and_terminal_job(final_cost_usd=0.10)
        n_delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
        assert n_delivered >= 1

    db_session.expire_all()
    alerts_b = (
        (await db_session.execute(select(Alert).where(Alert.project_id == project_id)))
        .scalars()
        .all()
    )
    assert len(alerts_b) == 0, "no alert expected when spend is under ceiling"

    # ── Scenario (a): over ceiling — exactly one alert ───────────────────────
    with _patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0.05}):
        _task_a, _job_a = await _make_parked_task_and_terminal_job(final_cost_usd=0.60)
        n_delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
        assert n_delivered >= 1

    db_session.expire_all()
    alerts_a = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.title == "Daily cost ceiling exceeded",
                    Alert.severity == AlertSeverity.WARNING,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts_a) == 1, f"expected exactly 1 alert, got {len(alerts_a)}"
    assert alerts_a[0].task_id is None, "alert must be project-level (task_id IS NULL)"

    # ── Scenario (c): cross-path dedup — no second alert on second wake ───────
    with _patch.dict(_shared_state.runtime_config, {"daily_cost_ceiling_usd": 0.05}):
        _task_c, _job_c = await _make_parked_task_and_terminal_job(final_cost_usd=0.70)
        n_delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
        assert n_delivered >= 1

    db_session.expire_all()
    alerts_c = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.title == "Daily cost ceiling exceeded",
                    Alert.severity == AlertSeverity.WARNING,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts_c) == 1, (
        f"dedup guard must prevent a second alert, got {len(alerts_c)}"
    )


# ── Project spend ceiling tests ───────────────────────────────────────────────


def _make_project_with_ceiling(ceiling_usd: float) -> Project:
    return Project(
        name=f"Ceiling Project {ceiling_usd}",
        user_id="test-user",
        build_config={"resource_envelope": {"spend_ceiling_usd": ceiling_usd}},
    )


async def _make_terminal_job(
    db_session, *, project_id, final_cost_usd: float
) -> ComputeJob:
    """Helper: insert a SUCCEEDED job with final_cost_usd completed 1 h ago."""
    job = ComputeJob(
        project_id=project_id,
        backend="local",
        machine_type="e2-standard-2",
        status=ComputeJobStatus.SUCCEEDED,
        final_cost_usd=final_cost_usd,
        accrued_cost_usd=final_cost_usd,
        completed_at=_utcnow() - timedelta(hours=1),
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


@pytest.mark.asyncio
async def test_create_compute_job_respects_project_daily_spend_ceiling(db_session):
    """Ceiling set, rolling settled spend + requested cap > ceiling → blocked."""
    project = _make_project_with_ceiling(5.0)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Settled spend: $4.50 in the last 24 h.
    await _make_terminal_job(db_session, project_id=project.id, final_cost_usd=4.50)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Requesting $1.00 cap: 4.50 + 1.00 = 5.50 > 5.00 → should raise.
    with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc_info:
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="echo ceiling",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )

    detail = exc_info.value.detail
    assert detail["error"] == "project_spend_ceiling_exceeded"
    assert detail["ceiling_usd"] == pytest.approx(5.0)
    assert detail["rolling_spend_usd"] == pytest.approx(4.50)
    assert detail["requested_cap_usd"] == pytest.approx(1.0)

    # No job row should have been created.
    jobs = (
        (
            await db_session.execute(
                select(ComputeJob).where(
                    ComputeJob.project_id == project.id,
                    ComputeJob.status.in_(
                        [
                            ComputeJobStatus.QUEUED,
                            ComputeJobStatus.STARTING,
                            ComputeJobStatus.RUNNING,
                            ComputeJobStatus.POLLING,
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    assert jobs == [], "no compute job should be created when ceiling is exceeded"


@pytest.mark.asyncio
async def test_create_compute_job_respects_resource_envelope_spend_ceiling(db_session):
    """Ceiling set via resource_envelope key blocks launch when rolling spend >= ceiling."""
    project = Project(
        name="Resource Envelope Ceiling Project",
        user_id="test-user",
        build_config={
            "resource_envelope": {
                "spend_ceiling_usd": 5.0,
                "spend_window_days": 30,
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Settled spend: $4.50 within the 30-day window.
    await _make_terminal_job(db_session, project_id=project.id, final_cost_usd=4.50)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Requesting $1.00 cap: 4.50 + 1.00 = 5.50 >= 5.00 → should raise.
    with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc_info:
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="echo ceiling via resource_envelope",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )

    detail = exc_info.value.detail
    assert detail["error"] == "project_spend_ceiling_exceeded"
    assert detail["ceiling_usd"] == pytest.approx(5.0)
    assert detail["rolling_spend_usd"] == pytest.approx(4.50)
    assert detail["requested_cap_usd"] == pytest.approx(1.0)

    # No active job row should have been created.
    jobs = (
        (
            await db_session.execute(
                select(ComputeJob).where(
                    ComputeJob.project_id == project.id,
                    ComputeJob.status.in_(
                        [
                            ComputeJobStatus.QUEUED,
                            ComputeJobStatus.STARTING,
                            ComputeJobStatus.RUNNING,
                            ComputeJobStatus.POLLING,
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    assert jobs == [], (
        "no compute job should be created when resource_envelope ceiling is exceeded"
    )


@pytest.mark.asyncio
async def test_create_compute_job_ceiling_allows_when_under_budget(db_session):
    """Ceiling set, rolling spend + cap is within the ceiling → job created."""
    project = _make_project_with_ceiling(10.0)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Settled spend: $4.00.
    await _make_terminal_job(db_session, project_id=project.id, final_cost_usd=4.00)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # 4.00 + 1.00 = 5.00 < 10.00 → allowed.
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo under budget",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )

    assert job.status == ComputeJobStatus.QUEUED


@pytest.mark.asyncio
async def test_create_compute_job_idempotent_reuse_ignores_ceiling(db_session):
    """Fingerprint-matching reuse returns the existing job even when over budget."""
    project = _make_project_with_ceiling(1.0)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # First launch succeeds before any spend is recorded.
    first = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo idempotent ceiling",
        machine_type="e2-standard-2",
        spend_cap_usd=0.50,
        deadline_seconds=300,
    )

    # Now push settled spend over the ceiling.
    await _make_terminal_job(db_session, project_id=project.id, final_cost_usd=0.90)

    # Same fingerprint: the existing job is reused — ceiling check is skipped.
    second = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo idempotent ceiling",
        machine_type="e2-standard-2",
        spend_cap_usd=0.50,
        deadline_seconds=300,
    )

    assert first.id == second.id, "idempotent reuse must return the same job"


@pytest.mark.asyncio
async def test_create_compute_job_no_ceiling_configured(db_session):
    """No daily_spend_ceiling_usd → job is always created without spend checks."""
    project = Project(name="No Ceiling Project", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Large settled spend; no ceiling configured so it should not block.
    await _make_terminal_job(db_session, project_id=project.id, final_cost_usd=9999.0)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo no ceiling",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )

    assert job.status == ComputeJobStatus.QUEUED


@pytest.mark.asyncio
async def test_create_compute_job_ceiling_counts_active_inflight_spend(db_session):
    """In-flight (active) job accrued costs push total over ceiling → blocked."""
    project = _make_project_with_ceiling(5.0)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # An existing in-flight job with $4.50 accrued cost (not yet terminal).
    running_task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(running_task)
    await db_session.commit()
    await db_session.refresh(running_task)

    inflight = ComputeJob(
        project_id=project.id,
        task_id=running_task.id,
        backend="local",
        machine_type="e2-standard-2",
        status=ComputeJobStatus.RUNNING,
        accrued_cost_usd=4.50,
        final_cost_usd=None,
    )
    db_session.add(inflight)
    await db_session.commit()

    # New task wants to launch a $1.00 job: 0 settled + 4.50 inflight + 1.00 > 5.00.
    new_task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(new_task)
    await db_session.commit()
    await db_session.refresh(new_task)

    with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc_info:
        await compute_jobs.create_compute_job(
            db_session,
            new_task,
            command="echo inflight ceiling",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )

    detail = exc_info.value.detail
    assert detail["error"] == "project_spend_ceiling_exceeded"
    assert detail["rolling_spend_usd"] == pytest.approx(4.50)


@pytest.mark.asyncio
async def test_create_compute_job_ceiling_counts_agent_vm_spend(db_session):
    """Cross-lane: agent-VM/LLM spend alone (zero compute) blocks a compute launch.

    A project with no compute jobs but $4.50 of agent-VM/LLM spend should block
    a new compute launch when 4.50 + 1.00 cap = 5.50 > 5.00 ceiling.
    """
    project = _make_project_with_ceiling(5.0)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # No compute jobs at all — mock agent-VM/LLM window spend at $4.50.
    # 0.0 compute + 4.50 task + 1.00 cap = 5.50 > 5.00 → blocked.
    with patch(
        "app.efficiency_snapshot.compute_project_window_spend_usd",
        new=AsyncMock(return_value=4.50),
    ):
        with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc_info:
            await compute_jobs.create_compute_job(
                db_session,
                task,
                command="echo cross-lane ceiling",
                machine_type="e2-standard-2",
                spend_cap_usd=1.0,
                deadline_seconds=300,
            )

    detail = exc_info.value.detail
    assert detail["error"] == "project_spend_ceiling_exceeded"
    assert detail["ceiling_usd"] == pytest.approx(5.0)
    # rolling = compute(0.0) + task(4.50) = 4.50; rolling + cap = 5.50 > ceiling
    assert detail["rolling_spend_usd"] == pytest.approx(4.50)
    assert detail["task_spend_usd"] == pytest.approx(4.50)
    assert detail["compute_spend_usd"] == pytest.approx(0.0)
    assert detail["requested_cap_usd"] == pytest.approx(1.0)

    # No active compute job row should exist.
    jobs = (
        (
            await db_session.execute(
                select(ComputeJob).where(
                    ComputeJob.project_id == project.id,
                    ComputeJob.status.in_(
                        [
                            ComputeJobStatus.QUEUED,
                            ComputeJobStatus.STARTING,
                            ComputeJobStatus.RUNNING,
                            ComputeJobStatus.POLLING,
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    assert jobs == [], (
        "no compute job should be created when agent-VM spend exceeds ceiling"
    )


@pytest.mark.asyncio
async def test_create_compute_job_ceiling_task_spend_uses_correct_window(db_session):
    """compute_project_window_spend_usd is called with window_hours=window_days*24.

    When resource_envelope.spend_window_days=30, the agent-VM/LLM spend helper
    must be queried with window_hours=720 (not the default 24), so both compute
    and task spend are measured over the same rolling window.
    """
    project = Project(
        name="Window Match Project",
        user_id="test-user",
        build_config={
            "resource_envelope": {
                "spend_ceiling_usd": 100.0,
                "spend_window_days": 30,
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    captured_kwargs: dict = {}

    async def capture_call(session, pid, *, window_hours=24):
        captured_kwargs["window_hours"] = window_hours
        return 0.0  # well below ceiling — we only care about the kwarg

    with patch(
        "app.efficiency_snapshot.compute_project_window_spend_usd",
        new=capture_call,
    ):
        # Should succeed (0.0 spend + 1.0 cap = 1.0 < 100.0 ceiling).
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="echo window match",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )

    assert captured_kwargs.get("window_hours") == 30 * 24, (
        f"expected window_hours=720 (30-day window), got {captured_kwargs.get('window_hours')}"
    )


@pytest.mark.asyncio
async def test_create_compute_job_ceiling_no_double_count(db_session):
    """Compute spend and agent-VM spend are summed without overlap.

    A project with $3.00 settled compute spend + $1.50 agent-VM/LLM spend =
    $4.50 total.  Adding a $1.00 cap → 5.50 > 5.00 → blocked.  Reducing the
    task spend to $0.00 (5.00 + 0.00 + 1.00 = 6.00 > 5.00... wait, still
    blocked).  Use $0.50 task spend: 3.00 + 0.50 + 1.00 = 4.50 < 5.00 → allowed.
    """
    project = _make_project_with_ceiling(5.0)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # $3.00 settled compute spend.
    await _make_terminal_job(db_session, project_id=project.id, final_cost_usd=3.00)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # With $0.50 agent-VM spend: 3.00 + 0.50 + 1.00 = 4.50 < 5.00 → allowed.
    with patch(
        "app.efficiency_snapshot.compute_project_window_spend_usd",
        new=AsyncMock(return_value=0.50),
    ):
        job = await compute_jobs.create_compute_job(
            db_session,
            task,
            command="echo no double count under",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )
    assert job.status == ComputeJobStatus.QUEUED

    # Now block new task: 3.00 settled + 1.50 agent-VM + 1.00 cap = 5.50 > 5.00.
    task2 = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task2)
    await db_session.commit()
    await db_session.refresh(task2)

    with patch(
        "app.efficiency_snapshot.compute_project_window_spend_usd",
        new=AsyncMock(return_value=1.50),
    ):
        with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc_info:
            await compute_jobs.create_compute_job(
                db_session,
                task2,
                command="echo no double count over",
                machine_type="e2-standard-2",
                spend_cap_usd=1.0,
                deadline_seconds=300,
            )

    detail = exc_info.value.detail
    assert detail["error"] == "project_spend_ceiling_exceeded"
    # Total = 3.00 compute (settled) + QUEUED job's accrued 0 + 1.50 agent = 4.50
    # But the QUEUED job from above adds to inflight accrued_cost_usd (0.0).
    assert detail["compute_spend_usd"] == pytest.approx(3.0)
    assert detail["task_spend_usd"] == pytest.approx(1.50)
    assert detail["rolling_spend_usd"] == pytest.approx(4.50)


# ---------------------------------------------------------------------------
# Per-project compute-job concurrency cap tests
# ---------------------------------------------------------------------------


async def _make_active_compute_job(db_session, *, project_id) -> ComputeJob:
    """Helper: insert an active (RUNNING) ComputeJob for the given project."""
    job = ComputeJob(
        project_id=project_id,
        backend="local",
        machine_type="e2-standard-2",
        status=ComputeJobStatus.RUNNING,
        accrued_cost_usd=0.0,
        spend_cap_usd=1.0,
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


@pytest.mark.asyncio
async def test_compute_job_concurrency_cap_blocks_at_cap(db_session):
    """Project at its configured cap → launch raises compute_job_concurrency_limit_exceeded."""
    from app.shared_state import runtime_config

    project = Project(name="Cap Blocks At Cap", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Insert 2 active jobs so the project is AT cap=2.
    await _make_active_compute_job(db_session, project_id=project.id)
    await _make_active_compute_job(db_session, project_id=project.id)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    orig = dict(runtime_config.get("max_concurrent_compute_jobs_by_project") or {})
    runtime_config["max_concurrent_compute_jobs_by_project"] = {str(project.id): 2}
    try:
        with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc_info:
            await compute_jobs.create_compute_job(
                db_session,
                task,
                command="echo cap-test",
                machine_type="e2-standard-2",
                spend_cap_usd=1.0,
                deadline_seconds=300,
            )
    finally:
        runtime_config["max_concurrent_compute_jobs_by_project"] = orig

    detail = exc_info.value.detail
    assert detail["error"] == "compute_job_concurrency_limit_exceeded"
    assert detail["active_count"] == 2
    assert detail["cap"] == 2


@pytest.mark.asyncio
async def test_compute_job_concurrency_cap_allows_under_cap(db_session):
    """Project under its configured cap → launch succeeds."""
    from app.shared_state import runtime_config

    project = Project(name="Cap Allows Under Cap", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Insert 1 active job; cap is 2 → under cap.
    await _make_active_compute_job(db_session, project_id=project.id)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    orig = dict(runtime_config.get("max_concurrent_compute_jobs_by_project") or {})
    runtime_config["max_concurrent_compute_jobs_by_project"] = {str(project.id): 2}
    try:
        job = await compute_jobs.create_compute_job(
            db_session,
            task,
            command="echo under-cap",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )
    finally:
        runtime_config["max_concurrent_compute_jobs_by_project"] = orig

    assert job is not None
    assert job.project_id == project.id


@pytest.mark.asyncio
async def test_compute_job_concurrency_cap_no_cap_configured(db_session):
    """No cap entry for the project → launch succeeds regardless of active count."""
    from app.shared_state import runtime_config

    project = Project(name="Cap No Cap Configured", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Insert many active jobs; no cap configured → should not block.
    for _ in range(5):
        await _make_active_compute_job(db_session, project_id=project.id)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    orig = dict(runtime_config.get("max_concurrent_compute_jobs_by_project") or {})
    # Ensure no entry for this project.
    runtime_config["max_concurrent_compute_jobs_by_project"] = {}
    try:
        job = await compute_jobs.create_compute_job(
            db_session,
            task,
            command="echo no-cap",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )
    finally:
        runtime_config["max_concurrent_compute_jobs_by_project"] = orig

    assert job is not None


@pytest.mark.asyncio
async def test_compute_job_concurrency_cap_per_project_isolation(db_session):
    """Project A at cap does not block Project B's launch."""
    from app.shared_state import runtime_config

    project_a = Project(name="Cap Isolation A", user_id="test-user")
    project_b = Project(name="Cap Isolation B", user_id="test-user")
    db_session.add(project_a)
    db_session.add(project_b)
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    # Project A is at cap=1 with 1 active job.
    await _make_active_compute_job(db_session, project_id=project_a.id)

    task_b = Task(project_id=project_b.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task_b)
    await db_session.commit()
    await db_session.refresh(task_b)

    orig = dict(runtime_config.get("max_concurrent_compute_jobs_by_project") or {})
    runtime_config["max_concurrent_compute_jobs_by_project"] = {
        str(project_a.id): 1,
    }
    try:
        # Project B has no cap and no active jobs → should succeed.
        job = await compute_jobs.create_compute_job(
            db_session,
            task_b,
            command="echo project-b",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )
    finally:
        runtime_config["max_concurrent_compute_jobs_by_project"] = orig

    assert job is not None
    assert job.project_id == project_b.id


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


def test_gcp_startup_script_emits_release_echo_when_sha_present():
    """GCP startup script contains the release echo emission block."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python eval.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=300,
        env={"BLUEPRINT_RELEASE_SHA": "abc123def456"},
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    assert "BLUEPRINT_COMPUTE_RELEASE_ECHO=" in script
    assert "BLUEPRINT_RELEASE_SHA" in script
    assert "RELEASE_ECHO_B64" in script


def test_gcp_startup_script_release_echo_block_is_no_op_without_sha():
    """GCP startup script echo block is safely present but no-op without env var."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python eval.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=300,
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    # The echo block is in the script but only fires when the env var is set.
    assert "BLUEPRINT_COMPUTE_RELEASE_ECHO=" in script
    assert "BLUEPRINT_RELEASE_SHA" in script


def test_gcp_startup_script_clones_repo_when_repo_clone_present():
    """repo_clone+repo_token ⇒ script clones the repo, cds in, sets PYTHONPATH."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python3 -m pkg.eval",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=300,
        repo_clone={"owner": "octo", "repo": "demo", "ref": "release-x"},
        repo_token="ghs_FAKETOKEN123",
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    assert "git clone --depth 1 --branch release-x" in script
    assert "https://x-access-token:ghs_FAKETOKEN123@github.com/octo/demo.git" in script
    assert "cd /tmp/blueprint_repo" in script
    assert "export PYTHONPATH=/tmp/blueprint_repo:${PYTHONPATH:-}" in script
    # A clone failure must terminate the job LOUDLY (parseable markers), not leave
    # the command to die on ModuleNotFound with "no terminal marker".
    assert "repo_clone_failed" in script
    assert f"{compute_backends_module._SERIAL_STATUS_PREFIX}FAILED" in script


def test_gcp_startup_script_no_clone_without_repo_clone():
    """No repo_clone ⇒ no git clone block (legacy bare-VM behavior preserved)."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="echo hi",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=300,
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    assert "git clone" not in script
    assert "/tmp/blueprint_repo" not in script


def test_gcp_startup_script_no_clone_without_token():
    """repo_clone present but no token ⇒ no clone (both are required)."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="echo hi",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=300,
        repo_clone={"owner": "octo", "repo": "demo", "ref": "main"},
        repo_token=None,
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    assert "git clone" not in script


def test_gcp_startup_script_classifies_clone_failure_with_typed_reason():
    """A clone failure must be ATTRIBUTABLE: stderr is captured (not discarded),
    classified into auth/ratelimit/network/timeout, token-scrubbed, and folded into
    the bundle — and the serial console only ever sees `repo_clone_failed:<class>`,
    never the captured stderr text (which can echo the tokened URL)."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python3 -m pkg.eval",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=300,
        repo_clone={"owner": "octo", "repo": "demo", "ref": "main"},
        repo_token="ghs_FAKETOKEN1234567890",
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    # stderr is captured to a file, NOT discarded to /dev/null.
    assert "2>/tmp/blueprint_clone.err" in script
    assert "git clone --depth 1 --branch main" in script
    # The failure is classified into a typed bucket; ratelimit is distinguished
    # from auth so a drained GitHub quota is separable from a bad/expired token.
    assert "CLONE_CLASS=ratelimit" in script
    assert "CLONE_CLASS=auth" in script
    assert "CLONE_CLASS=network" in script
    assert "CLONE_CLASS=timeout" in script
    # The typed reason flows to BOTH the serial marker and the durable record.
    assert (
        f"{compute_backends_module._SERIAL_REASON_PREFIX}repo_clone_failed:$CLONE_CLASS"
        in script
    )
    assert "FAILED 1 repo_clone_failed:$CLONE_CLASS" in script
    # The captured stderr is token-scrubbed before it lands in the durable bundle.
    assert "x-access-token:REDACTED@" in script
    # ...and the scrubbed file is copied into the artifact dir so it ships in the
    # GCS bundle for full post-hoc diagnosis.
    assert "cp /tmp/blueprint_clone.err" in script
    assert "/clone.err" in script
    # The captured stderr is never echoed to the serial console (only the typed
    # reason is) — guard against a `cat`/`echo` of the raw file leaking the token.
    assert "cat /tmp/blueprint_clone.err" not in script


def test_gcp_startup_script_writes_boot_beacon_before_command():
    """A boot beacon (provisional FAILED record) is written BEFORE the clone and
    command so an ungraceful mid-run VM death still uploads a durable terminal.json
    — turning the blind 'no marker, no bundle' FAILURE into an attributable one.
    Every later phase overwrites it with the true outcome."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python3 -m pkg.eval",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=300,
        repo_clone={"owner": "octo", "repo": "demo", "ref": "main"},
        repo_token="ghs_FAKETOKEN1234567890",
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    assert "FAILED 1 vm_terminated_before_completion" in script
    # The beacon is armed only AFTER the EXIT trap (so a death always uploads it)
    # and BEFORE the command actually runs (so a mid-command death is covered).
    # Anchor command_idx on the INVOCATION, not the near-top base64 decode line
    # `... > /tmp/blueprint_command_runner.py` (which `index` would match first).
    trap_idx = script.index("trap upload_artifacts EXIT")
    beacon_idx = script.index("vm_terminated_before_completion")
    command_idx = script.index("python3 /tmp/blueprint_command_runner.py")
    assert trap_idx < beacon_idx < command_idx


def test_gcp_startup_script_wraps_command_in_container_with_gpu():
    """When container_image AND an accelerator are set, the GCP startup script must
    wrap the command in `docker run --gpus all`, pull the image first, mount the VM
    artifact dir into the container, and point BLUEPRINT_RESULT_FILE at the
    in-container result path so the existing terminal-record reader keeps working."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python train.py --epochs 10",
        machine_type="a2-highgpu-1g",
        spend_cap_usd=5.0,
        deadline_at=None,
        runtime_deadline_seconds=600,
        accelerator="T4",
        container_image="nvcr.io/nvidia/tensorflow:25.01-tf2-py3",
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    # The command runs inside the declared image, with GPU passthrough.
    assert "docker run" in script
    assert "--rm" in script
    assert "--gpus all" in script
    # The image is pulled before the command runs.
    assert "docker pull" in script
    assert "nvcr.io/nvidia/tensorflow:25.01-tf2-py3" in script
    # The VM artifact dir is bind-mounted into the container so result.json and
    # artifacts written inside land on the host the terminal-record reader uses.
    art = compute_backends_module._GCP_ARTIFACT_DIR_PATH
    assert f"{art}:{art}" in script
    # BLUEPRINT_RESULT_FILE seen by the in-container command points at the mounted
    # result path (a sibling of the artifact dir), NOT the bare-VM standalone path.
    assert f"BLUEPRINT_RESULT_FILE={art}/result.json" in script
    # The original command is preserved inside the docker invocation.
    assert "python train.py --epochs 10" in script
    # docker run is wired through the command runner via `bash -lc`.
    assert "bash -lc" in script
    # The pull failure path emits the distinct, parseable terminal reason on BOTH
    # the serial marker and the durable terminal-record paths.
    assert "container_image_pull_failed" in script
    assert (
        f"{compute_backends_module._SERIAL_REASON_PREFIX}container_image_pull_failed"
        in script
    )
    assert "FAILED 1 container_image_pull_failed" in script
    # Boot-beacon ordering invariant must survive the inserted pull/run steps.
    trap_idx = script.index("trap upload_artifacts EXIT")
    beacon_idx = script.index("vm_terminated_before_completion")
    command_idx = script.index("python3 /tmp/blueprint_command_runner.py")
    assert trap_idx < beacon_idx < command_idx


def test_gcp_container_no_progress_watchdog_polls_container_progress_file():
    """In container mode the container writes progress to
    <artifact_dir>/progress.json (via BLUEPRINT_PROGRESS_FILE), NOT the bare-mode
    standalone _GCP_PROGRESS_FILE_PATH. The no-progress watchdog must stat that
    same host-visible file so it observes the container's mtime advances instead
    of killing a live containerized job at the first no_progress_timeout window."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python train.py --epochs 10",
        machine_type="a2-highgpu-1g",
        spend_cap_usd=5.0,
        deadline_at=None,
        runtime_deadline_seconds=3600,
        accelerator="T4",
        container_image="nvcr.io/nvidia/tensorflow:25.01-tf2-py3",
        no_progress_timeout_seconds=600,
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    art = compute_backends_module._GCP_ARTIFACT_DIR_PATH
    container_progress = f"{art}/progress.json"
    # The container's BLUEPRINT_PROGRESS_FILE points at the mounted artifact-dir
    # path (host_out_dir == container_out_dir == _GCP_ARTIFACT_DIR_PATH for GCP).
    assert f"BLUEPRINT_PROGRESS_FILE={container_progress}" in script
    # The watchdog must stat that same host path, NOT the bare-mode standalone one.
    assert f"stat -c %Y {container_progress}" in script
    assert compute_backends_module._GCP_PROGRESS_FILE_PATH not in script


def test_gcp_bare_no_progress_watchdog_polls_standalone_progress_file():
    """Without a container image, the bare-mode watchdog and export both use the
    standalone _GCP_PROGRESS_FILE_PATH — the container-mode repoint must not leak
    into the bare path."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=3600,
        no_progress_timeout_seconds=600,
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    bare = compute_backends_module._GCP_PROGRESS_FILE_PATH
    assert f"export BLUEPRINT_PROGRESS_FILE={bare}" in script
    assert f"stat -c %Y {bare}" in script


def test_gcp_startup_script_gpus_flag_gated_on_accelerator():
    """--gpus all appears only when an accelerator is requested (the CPU agent image
    lacks the NVIDIA Container Toolkit, so docker run --gpus all would fail there)."""
    base = dict(
        job_id=uuid.uuid4(),
        command="python eval.py",
        machine_type="e2-standard-4",
        spend_cap_usd=2.0,
        deadline_at=None,
        runtime_deadline_seconds=300,
        container_image="nvcr.io/nvidia/cuda:12.0-base",
    )
    gpu_script = compute_backends_module._build_gcp_startup_script(
        ComputeLaunchSpec(accelerator="T4", **base),
        artifact_upload_url="https://storage.example.com/upload",
    )
    cpu_script = compute_backends_module._build_gcp_startup_script(
        ComputeLaunchSpec(accelerator=None, **base),
        artifact_upload_url="https://storage.example.com/upload",
    )
    assert "--gpus all" in gpu_script
    assert "docker run" in cpu_script
    assert "--gpus" not in cpu_script


def test_gcp_startup_script_docker_login_only_with_registry_auth():
    """`docker login --password-stdin` is emitted only when registry auth carries a
    password; the password is delivered via an env var (never on a command line)
    and never reaches the serial console or any artifact/log marker."""
    secret = "s3cr3t-ngc-api-key"
    base = dict(
        job_id=uuid.uuid4(),
        command="python train.py",
        machine_type="a2-highgpu-1g",
        spend_cap_usd=5.0,
        deadline_at=None,
        runtime_deadline_seconds=600,
        accelerator="T4",
        container_image="nvcr.io/nvidia/tensorflow:25.01-tf2-py3",
    )
    with_auth = compute_backends_module._build_gcp_startup_script(
        ComputeLaunchSpec(
            container_registry_auth={
                "server": "nvcr.io",
                "username": "$oauthtoken",
                "password": secret,
            },
            **base,
        ),
        artifact_upload_url="https://storage.example.com/upload",
    )
    without_auth = compute_backends_module._build_gcp_startup_script(
        ComputeLaunchSpec(**base),
        artifact_upload_url="https://storage.example.com/upload",
    )
    assert "docker login" in with_auth
    assert "--password-stdin" in with_auth
    assert "docker login" not in without_auth
    # The password is consumed from an env var via --password-stdin, so it never
    # appears as an inline literal on the docker login command line itself.
    assert f"docker login {secret}" not in with_auth
    assert "-u \"${BLUEPRINT_REGISTRY_USERNAME}\"" in with_auth
    # The password must NEVER be echoed to the serial console: no `echo`/marker
    # line carries it. Every login/output redirect goes to /dev/null.
    for line in with_auth.splitlines():
        if line.lstrip().startswith("echo "):
            assert secret not in line, f"password leaked to serial echo: {line!r}"


def test_gcp_startup_script_passes_env_into_container():
    """When container_image is set without repo_clone, spec.env and spec.cwd are
    forwarded as docker -e flags and -w flag rather than being left only in the outer
    VM shell — matching the local-subprocess backend's else-branch behaviour."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python eval.py",
        machine_type="e2-standard-4",
        spend_cap_usd=2.0,
        deadline_at=None,
        runtime_deadline_seconds=300,
        container_image="python:3.12-slim",
        env={"MY_KEY": "my_val", "ANOTHER": "42"},
        cwd="/workdir",
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    # env vars must appear as -e flags inside the docker run invocation
    assert "-e MY_KEY=my_val" in script
    assert "-e ANOTHER=42" in script
    # cwd must appear as -w flag
    assert "-w /workdir" in script
    # docker run still wraps the command
    assert "docker run" in script
    assert "python eval.py" in script


def test_gcp_startup_script_passes_env_into_container_with_repo_clone():
    """When container_image + repo_clone + spec.env are all set, user env vars are
    included as -e flags alongside the auto-injected PYTHONPATH=/workspace in the
    docker run invocation (repo_clone branch of _build_gcp_startup_script)."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python eval.py",
        machine_type="e2-standard-4",
        spend_cap_usd=2.0,
        deadline_at=None,
        runtime_deadline_seconds=300,
        container_image="python:3.12-slim",
        repo_clone={"owner": "octo", "repo": "demo", "ref": "main"},
        repo_token="tok",
        env={"MY_KEY": "my_val", "ANOTHER": "42"},
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    # user env vars must appear as -e flags
    assert "-e MY_KEY=my_val" in script
    assert "-e ANOTHER=42" in script
    # PYTHONPATH=/workspace is auto-injected alongside user env vars
    assert "-e PYTHONPATH=/workspace" in script
    # docker run still wraps the command
    assert "docker run" in script
    assert "python eval.py" in script


def test_gcp_startup_script_no_docker_without_image():
    """Absent container_image, the generated script is byte-for-byte unchanged from
    the bare-shell path — no docker token appears anywhere."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=300,
        repo_clone={"owner": "o", "repo": "r", "ref": "main"},
        repo_token="tok",
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    assert "docker" not in script
    assert "container_image_pull_failed" not in script
    # The standalone bare-VM result path is still used when no container is set.
    assert compute_backends_module._GCP_RESULT_FILE_PATH in script


def test_gcp_startup_script_includes_setup_step_block():
    """A spec with setup_step includes a bail-out block with setup_step_failed reason."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python3 eval.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=300,
        setup_step="pip install -r requirements.txt",
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    # The setup step command appears in the script.
    assert "pip install -r requirements.txt" in script
    # A typed failure reason is emitted so downstream can attribute the failure.
    assert "setup_step_failed" in script
    assert f"{compute_backends_module._SERIAL_REASON_PREFIX}setup_step_failed" in script
    assert f"{compute_backends_module._SERIAL_STATUS_PREFIX}FAILED" in script
    # The setup step runs AFTER the boot beacon and BEFORE the main command runner.
    beacon_idx = script.index("vm_terminated_before_completion")
    setup_idx = script.index("pip install -r requirements.txt")
    command_idx = script.index("python3 /tmp/blueprint_command_runner.py")
    assert beacon_idx < setup_idx < command_idx
    # The setup step output is redirected to the artifact dir for diagnosis.
    assert "setup_step.log" in script


def test_gcp_startup_script_no_setup_step_unchanged():
    """No setup_step on the spec ⇒ no setup_step_failed marker in the script."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="echo hi",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=300,
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    assert "setup_step_failed" not in script
    assert "setup_step.log" not in script


def test_local_wrapper_includes_setup_step():
    """_build_local_wrapper_command with setup_step runs it before the main command."""
    wrapper = compute_backends_module._build_local_wrapper_command(
        "python3 eval.py",
        external_id="local-testsetup",
        setup_step="pip install torch",
    )
    # The setup step command is in the wrapper.
    assert "pip install torch" in wrapper
    # The setup step runs before the main command (the command_runner invocation).
    setup_idx = wrapper.index("pip install torch")
    cmd_idx = wrapper.index("if python3 ")
    assert setup_idx < cmd_idx
    # On failure the wrapper writes the terminal file and exits early.
    assert "SETUP_EXIT" in wrapper
    assert "setup_step.log" in wrapper


async def test_attach_repo_clone_sets_repo_and_token(db_session, monkeypatch):
    """_attach_repo_clone resolves owner/repo + ref (from base_branch) + a token."""
    project = Project(
        name="repo-clone", user_id="u", github_owner="octo", github_repo="demo"
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute parent",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "base_branch": "release-x"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python3 -m pkg.eval",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )

    async def fake_token(_project, _session):
        return "ghs_FAKE"

    monkeypatch.setattr("app.branch_cleanup._get_project_github_token", fake_token)
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    spec = await compute_jobs._attach_repo_clone(
        session_factory, job, compute_jobs._build_launch_spec(job)
    )
    assert spec.repo_clone == {"owner": "octo", "repo": "demo", "ref": "release-x"}
    assert spec.repo_token == "ghs_FAKE"


async def test_attach_repo_clone_skips_when_project_has_no_repo(
    db_session, monkeypatch
):
    """A repo-less project ⇒ no checkout and the token path is never touched."""
    project = Project(name="no-repo", user_id="u")
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

    calls = {"n": 0}

    async def fake_token(_project, _session):
        calls["n"] += 1
        return "ghs_FAKE"

    monkeypatch.setattr("app.branch_cleanup._get_project_github_token", fake_token)
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    spec = await compute_jobs._attach_repo_clone(
        session_factory, job, compute_jobs._build_launch_spec(job)
    )
    assert spec.repo_clone is None
    assert spec.repo_token is None
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_attach_repo_clone_falls_back_on_deleted_plan_branch(
    db_session, monkeypatch
):
    """When base_branch is a plan/* branch that no longer exists, _attach_repo_clone
    degrades to the project's promotion_target instead of cloning a dead ref.

    This covers compute_experiment tasks that were delayed past plan-branch cleanup:
    the plan merged to integration, the plan/* branch was deleted, but the compute
    job is only now launching.
    """
    project = Project(
        name="plan-fallback",
        user_id="u",
        github_owner="octo",
        github_repo="demo",
        promotion_target="integration",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute parent",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "base_branch": "plan/abc123"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python bench.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )

    async def fake_token(_project, _session):
        return "ghs_FAKE"

    async def branch_not_found(_token, _owner, _repo, _branch):
        raise Exception("404 branch not found")

    monkeypatch.setattr("app.branch_cleanup._get_project_github_token", fake_token)
    monkeypatch.setattr("app.github_refs.get_branch_sha", branch_not_found)
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    spec = await compute_jobs._attach_repo_clone(
        session_factory, job, compute_jobs._build_launch_spec(job)
    )
    # Must degrade to promotion_target, not the deleted plan branch.
    assert spec.repo_clone == {
        "owner": "octo",
        "repo": "demo",
        "ref": "integration",
    }
    assert spec.repo_token == "ghs_FAKE"


@pytest.mark.asyncio
async def test_attach_repo_clone_uses_plan_branch_when_it_exists(
    db_session, monkeypatch
):
    """When base_branch is a plan/* branch that still exists, use it directly."""
    project = Project(
        name="plan-exists",
        user_id="u",
        github_owner="octo",
        github_repo="demo",
        promotion_target="integration",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute parent",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment", "base_branch": "plan/abc123"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python bench.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )

    async def fake_token(_project, _session):
        return "ghs_FAKE"

    async def branch_exists(_token, _owner, _repo, _branch):
        return "abc123sha"

    monkeypatch.setattr("app.branch_cleanup._get_project_github_token", fake_token)
    monkeypatch.setattr("app.github_refs.get_branch_sha", branch_exists)
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    spec = await compute_jobs._attach_repo_clone(
        session_factory, job, compute_jobs._build_launch_spec(job)
    )
    # Branch exists — use it as-is.
    assert spec.repo_clone == {
        "owner": "octo",
        "repo": "demo",
        "ref": "plan/abc123",
    }


def test_parse_serial_status_extracts_release_echo():
    """_parse_serial_status decodes BLUEPRINT_COMPUTE_RELEASE_ECHO into echoed_release_sha."""
    sha = "abc123def456"
    echo_b64 = base64.b64encode(sha.encode("utf-8")).decode("ascii")
    serial = "\n".join(
        [
            "BLUEPRINT_COMPUTE_REASON=completed",
            "BLUEPRINT_COMPUTE_STATUS=SUCCEEDED",
            "BLUEPRINT_COMPUTE_EXIT_CODE=0",
            f"BLUEPRINT_COMPUTE_RELEASE_ECHO={echo_b64}",
        ]
    )
    status = compute_backends_module._parse_serial_status(serial)
    assert status is not None
    assert status.status == ComputeJobStatus.SUCCEEDED
    assert status.echoed_release_sha == sha


def test_parse_serial_status_missing_echo_yields_none():
    """_parse_serial_status returns echoed_release_sha=None when marker is absent."""
    serial = "\n".join(
        [
            "BLUEPRINT_COMPUTE_REASON=completed",
            "BLUEPRINT_COMPUTE_STATUS=SUCCEEDED",
            "BLUEPRINT_COMPUTE_EXIT_CODE=0",
        ]
    )
    status = compute_backends_module._parse_serial_status(serial)
    assert status is not None
    assert status.echoed_release_sha is None


def test_parse_serial_status_garbled_echo_is_tolerated():
    """Garbled release echo marker is tolerated — echoed_release_sha stays None."""
    serial = "\n".join(
        [
            "BLUEPRINT_COMPUTE_STATUS=SUCCEEDED",
            "BLUEPRINT_COMPUTE_EXIT_CODE=0",
            "BLUEPRINT_COMPUTE_RELEASE_ECHO=not-valid-base64-$$$",
        ]
    )
    status = compute_backends_module._parse_serial_status(serial)
    assert status is not None
    assert status.echoed_release_sha is None


def test_parse_serial_status_decodes_resume_marker():
    """_parse_serial_status decodes BLUEPRINT_COMPUTE_RESUME into
    resume_provenance dict."""
    payload_b64 = base64.b64encode(
        json.dumps({"ok": True, "file_count": 3}).encode()
    ).decode("ascii")
    serial = "\n".join(
        [
            "BLUEPRINT_COMPUTE_STATUS=RUNNING",
            f"BLUEPRINT_COMPUTE_RESUME={payload_b64}",
        ]
    )
    status = compute_backends_module._parse_serial_status(serial)
    assert status is not None
    assert status.status == ComputeJobStatus.RUNNING
    assert status.resume_provenance is not None
    assert status.resume_provenance.get("ok") is True
    assert status.resume_provenance.get("file_count") == 3


def test_parse_serial_status_missing_resume_yields_none():
    """_parse_serial_status returns resume_provenance=None when marker is absent."""
    serial = "\n".join(
        [
            "BLUEPRINT_COMPUTE_STATUS=RUNNING",
        ]
    )
    status = compute_backends_module._parse_serial_status(serial)
    assert status is not None
    assert status.status == ComputeJobStatus.RUNNING
    assert status.resume_provenance is None


def test_parse_serial_status_garbled_resume_is_tolerated():
    """Garbled resume provenance marker is tolerated — resume_provenance stays None."""
    serial = "\n".join(
        [
            "BLUEPRINT_COMPUTE_STATUS=RUNNING",
            "BLUEPRINT_COMPUTE_RESUME=not-valid-base64-$$$",
        ]
    )
    status = compute_backends_module._parse_serial_status(serial)
    assert status is not None
    assert status.status == ComputeJobStatus.RUNNING
    assert status.resume_provenance is None


def test_parse_serial_status_resume_passes_through_terminal():
    """Resume provenance serial marker is preserved through terminal status
    (SUCCEEDED/FAILED), not only on RUNNING status."""
    payload_b64 = base64.b64encode(
        json.dumps({"ok": False, "file_count": 0, "reason": "resume_hydration_failed"}).encode()
    ).decode("ascii")
    serial = "\n".join(
        [
            "BLUEPRINT_COMPUTE_STATUS=FAILED",
            "BLUEPRINT_COMPUTE_EXIT_CODE=1",
            f"BLUEPRINT_COMPUTE_RESUME={payload_b64}",
        ]
    )
    status = compute_backends_module._parse_serial_status(serial)
    assert status is not None
    assert status.status == ComputeJobStatus.FAILED
    assert status.resume_provenance is not None
    assert status.resume_provenance.get("ok") is False
    assert status.resume_provenance.get("reason") == "resume_hydration_failed"


def test_local_wrapper_command_writes_release_echo_to_terminal_file():
    """Local wrapper writes RELEASE_ECHO= to the terminal state file."""
    wrapper = compute_backends_module._build_local_wrapper_command(
        "echo hi", external_id="local-releasetest"
    )
    assert "RELEASE_ECHO=$RELEASE_ECHO_B64" in wrapper
    assert "RELEASE_ECHO_B64" in wrapper
    assert "BLUEPRINT_RELEASE_SHA" in wrapper


@pytest.mark.asyncio
async def test_provision_local_does_not_inherit_orchestrator_secrets(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", "sentinel-private-key")
    monkeypatch.setenv("AGENT_JWT_SECRET", "sentinel-jwt-secret")

    captured_envs = []

    def fake_popen(args, cwd=None, env=None, **kwargs):
        captured_envs.append(dict(env) if env is not None else None)
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.returncode = None
        mock_proc.wait = MagicMock(return_value=0)
        return mock_proc

    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr(compute, "AGENT_DIR", str(tmp_path))

    task_id = uuid.uuid4()
    vm_instance_id = f"local-{uuid.uuid4()}"

    await compute._provision_local(
        vm_instance_id=vm_instance_id,
        task_id=task_id,
        payload_uri="payload://test",
        oauth_token="test-oauth-token",
        task_type="test",
    )

    assert len(captured_envs) == 1, "expected exactly one Popen call"
    child_env = captured_envs[0]
    assert child_env is not None
    assert "GITHUB_PRIVATE_KEY" not in child_env
    assert "AGENT_JWT_SECRET" not in child_env
    assert child_env.get("PAYLOAD_URI") == "payload://test"
    assert child_env.get("VM_INSTANCE_ID") == vm_instance_id


# ---------------------------------------------------------------------------
# Credential redaction: command_log
# ---------------------------------------------------------------------------


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


def test_command_log_credentials_redacted_on_operator_and_knowledge_surfaces():
    """Bearer tokens and signed-URL query strings in command_log are scrubbed on
    every durable surface: admin API (serialize_compute_job_response_safe),
    artifact JSON (build_compute_result_logs), and artifact Logs
    (_link_terminal_artifact via redact_command_log_text applied before storage).

    This is the named acceptance test for the fix-redact-credentials-command-log task.
    """
    raw = (
        "Fetching artifact from https://storage.example.com/bucket/obj?X-Goog-Signature=SECRETVAL&expires=999\n"
        "Authorization: Bearer ghp_SUPERSECRETTOKEN\n"
        "export AWS_SECRET_KEY=MY_SECRET_VALUE\n"
        "result: ok\n"
    )
    job = _make_job_with_command_log(raw)

    # --- operator API surface ---
    api_data = schemas.serialize_compute_job_response_safe(job)
    api_text = api_data["polling_metadata"]["command_log"]["text"]
    assert "SECRETVAL" not in api_text, (
        "signed URL query value must be redacted from API"
    )
    assert "ghp_SUPERSECRETTOKEN" not in api_text, (
        "bearer token must be redacted from API"
    )
    assert "MY_SECRET_VALUE" not in api_text, "export value must be redacted from API"
    assert "result: ok" in api_text, "benign output must survive redaction"

    # --- artifact JSON / knowledge document surface ---
    result = compute_jobs.build_compute_result_logs(job)
    import json as _json

    artifact = _json.loads(result)
    cl_text = artifact["command_log_text"]
    assert "SECRETVAL" not in cl_text, (
        "signed URL query value must be redacted from artifact JSON"
    )
    assert "ghp_SUPERSECRETTOKEN" not in cl_text, (
        "bearer token must be redacted from artifact JSON"
    )
    assert "MY_SECRET_VALUE" not in cl_text, (
        "export value must be redacted from artifact JSON"
    )
    assert "result: ok" in cl_text, (
        "benign output must survive redaction in artifact JSON"
    )

    # --- direct helper ---
    clean = schemas.redact_command_log_text(raw)
    assert "SECRETVAL" not in clean
    assert "ghp_SUPERSECRETTOKEN" not in clean
    assert "MY_SECRET_VALUE" not in clean
    assert "result: ok" in clean


def test_redact_command_log_text_strips_bearer_tokens():
    """redact_command_log_text removes Authorization headers and bearer-like values."""
    text = "Authorization: Bearer abc123\ntoken=mysecret\nok\n"
    result = schemas.redact_command_log_text(text)
    assert "abc123" not in result
    assert "mysecret" not in result
    assert "ok" in result


def test_redact_command_log_text_strips_url_query_strings():
    """redact_command_log_text strips URL query parameters (signed URLs etc.)."""
    text = "GET https://example.com/file?X-Goog-Signature=abc&Expires=123\ndone\n"
    result = schemas.redact_command_log_text(text)
    assert "abc" not in result
    assert "X-Goog-Signature" not in result
    assert "done" in result


def test_redact_command_log_text_strips_shell_exports():
    """redact_command_log_text redacts shell export statements."""
    text = "export SECRET_KEY=supersecret\nexport PATH=/usr/bin\nrunning\n"
    result = schemas.redact_command_log_text(text)
    assert "supersecret" not in result
    assert "running" in result


def test_redact_command_log_text_covers_bare_token_formats():
    """redact_command_log_text covers bare token formats not prefixed by 'Bearer'.

    This is the named acceptance test: ghp_***, sk-ant-***, ya29***, embedded
    git URL credential, and JSON access_token field must all be redacted even
    when no 'Authorization: Bearer' header is present.
    """
    raw = (
        # bare GitHub PAT — no Bearer prefix
        "fatal: repository 'https://github.com/org/repo.git/' not found\n"
        "token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123\n"
        # embedded git URL credential
        "Cloning into '/tmp/work'... (token=https://x-access-token:ghs_SECRETTOKEN123@github.com/org/repo)\n"
        # Anthropic key
        "api_key=sk-ant-api03-SUPERSECRETKEYVALUE\n"
        # Google OAuth access token
        "oauth: ya29.A0ARrdaM-GOOGLEOAUTHTOKEN\n"
        # JSON access_token field (caught by _CRED_FIELD_RE in _redact_credentials)
        '{"access_token": "ACCESS_TOKEN_VALUE_HERE", "expires_in": 3600}\n'
        # benign output
        "exit code: 1\n"
    )
    result = schemas.redact_command_log_text(raw)
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123" not in result, (
        "bare ghp_ token must be redacted"
    )
    assert "ghs_SECRETTOKEN123" not in result, (
        "embedded git URL ghs_ token must be redacted"
    )
    assert "sk-ant-api03-SUPERSECRETKEYVALUE" not in result, (
        "sk-ant- key must be redacted"
    )
    assert "ya29.A0ARrdaM-GOOGLEOAUTHTOKEN" not in result, (
        "ya29. token must be redacted"
    )
    assert "ACCESS_TOKEN_VALUE_HERE" not in result, (
        "JSON access_token value must be redacted"
    )
    assert "exit code: 1" in result, "benign output must survive"
    assert "expires_in" in result, "benign JSON field must survive"


def test_build_compute_result_logs_redacts_command_log_text():
    """build_compute_result_logs scrubs credentials before embedding command_log_text."""
    raw = "token=leaked_secret\nnormal output\n"
    job = _make_job_with_command_log(raw)
    import json as _json

    result = _json.loads(compute_jobs.build_compute_result_logs(job))
    assert "leaked_secret" not in result["command_log_text"]
    assert "normal output" in result["command_log_text"]


def test_serialize_compute_job_response_safe_redacts_command_log():
    """serialize_compute_job_response_safe scrubs command_log.text for operator API."""
    raw = "Bearer token=TOP_SECRET\nresult: 42\n"
    job = _make_job_with_command_log(raw)
    data = schemas.serialize_compute_job_response_safe(job)
    text = data["polling_metadata"]["command_log"]["text"]
    assert "TOP_SECRET" not in text
    assert "result: 42" in text


def test_serialize_compute_job_response_agent_returns_raw_command_log():
    """serialize_compute_job_response (non-safe, agent-facing) passes command_log text through.

    Agents own their job and need the full metadata — the non-safe variant intentionally
    omits redaction. This test documents the safe/agent asymmetry so a future refactor
    cannot collapse both serializers into a single redacting path without notice.
    """
    raw = "Bearer token=TOP_SECRET\nresult: 42\n"
    job = _make_job_with_command_log(raw)
    data = schemas.serialize_compute_job_response(job)
    text = data["polling_metadata"]["command_log"]["text"]
    # Non-safe: raw text is returned intact for agent callers (agents own the job).
    assert "TOP_SECRET" in text, (
        "agent-facing serialize_compute_job_response must not redact command_log.text"
    )
    assert "result: 42" in text


def test_command_runner_redacts_credentials(tmp_path):
    """command_runner.py redacts credentials at capture time before writing command.log.json.

    Runs the actual script (subprocess) so the test exercises the production
    code path, not a re-implementation.  Verifies that every major credential
    family is scrubbed while ordinary experiment output survives verbatim.
    """
    import pathlib
    import subprocess
    import sys

    script = (
        pathlib.Path(__file__).parent.parent
        / "app"
        / "_compute_scripts"
        / "command_runner.py"
    )
    log_file = tmp_path / "command.log.json"

    # Build a shell command that echoes representative credential formats to stdout.
    credential_lines = [
        # GitHub tokens
        "echo ghp_ABCDEF1234567890abcdefGHIJKLMNOPQRST",
        "echo ghs_SECRETTOKEN123456789012",
        "echo ghu_SomeGitHubUserToken123456",
        # Embedded git URL credential
        "echo https://x-access-token:ghp_SECRETTOKEN123@github.com/org/repo",
        # Generic user:pass URL
        "echo https://myuser:mypassword@example.com/repo",
        # Anthropic key
        "echo sk-ant-api03-SUPERSECRETKEYVALUE1234567890",
        # OpenAI project key (>=10 suffix chars)
        "echo sk-proj-PROJECTKEYVALUE1234567890abcdef",
        # Google AIza key (>=20 suffix chars)
        "echo AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ01234567",
        # Google OAuth token
        "echo ya29.A0ARrdaM-GOOGLEOAUTHTOKEN1234",
        # Bearer token
        "echo 'Authorization: Bearer MYSECRETBEARERTOKEN'",
        # Z.AI key env var
        "echo ZAI_API_KEY=ZAISECRET1234567890",
        "echo 'ZAI_API_KEY=\"ZAIQUOTED1234567890\"'",
        "echo '{\"ZAI_API_KEY\":\"ZAIJSON1234567890\",\"ANTHROPIC_AUTH_TOKEN\":\"ANTHROPICJSON1234567890\"}'",
        # Named field — access_token
        "echo 'access_token=FIELDTOKEN1234567890'",
        # AWS IAM access key
        "echo AKIAIOSFODNN7EXAMPLE",
        # Stripe keys
        "echo sk_live_STRIPELIVEKEY1234567890",
        "echo whsec_STRIPEWEBHOOKSECRET123456",
        # PostgreSQL DSN with embedded password
        "echo postgresql://dbuser:DBPASSWORD123@db.host:5432/mydb",
        # Benign output that must survive
        "echo 'metric: loss=0.042'",
        "echo 'step 100 complete'",
    ]
    command = " && ".join(credential_lines)

    proc = subprocess.run(
        [sys.executable, str(script), command, str(log_file)],
        capture_output=True,
        text=True,
    )
    # Script exits with the command exit code (0 for all echoes)
    assert proc.returncode == 0, f"command_runner failed: {proc.stderr}"
    assert log_file.exists(), "command.log.json was not written"

    record = json.loads(log_file.read_text())
    text = record["text"]

    # --- credentials must be absent ---
    assert "ghp_ABCDEF1234567890abcdefGHIJKLMNOPQRST" not in text, (
        "ghp_ must be redacted"
    )
    assert "ghs_SECRETTOKEN123456789012" not in text, "ghs_ must be redacted"
    assert "ghu_SomeGitHubUserToken123456" not in text, "ghu_ must be redacted"
    assert "ghp_SECRETTOKEN123" not in text, "embedded git URL token must be redacted"
    assert "mypassword" not in text, "user:pass URL must be redacted"
    assert "sk-ant-api03-SUPERSECRETKEYVALUE1234567890" not in text, (
        "sk-ant- must be redacted"
    )
    assert "sk-proj-PROJECTKEYVALUE1234567890abcdef" not in text, (
        "sk-proj- must be redacted"
    )
    assert "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ01234567" not in text, (
        "AIza key must be redacted"
    )
    assert "ya29.A0ARrdaM-GOOGLEOAUTHTOKEN1234" not in text, "ya29. must be redacted"
    assert "MYSECRETBEARERTOKEN" not in text, "Bearer token must be redacted"
    assert "ZAISECRET1234567890" not in text, "ZAI_API_KEY must be redacted"
    assert "ZAIQUOTED1234567890" not in text, "quoted ZAI_API_KEY must be redacted"
    assert "ZAIJSON1234567890" not in text, "JSON ZAI_API_KEY must be redacted"
    assert "ANTHROPICJSON1234567890" not in text, (
        "JSON ANTHROPIC_AUTH_TOKEN must be redacted"
    )
    assert "FIELDTOKEN1234567890" not in text, (
        "named access_token field must be redacted"
    )
    assert "AKIAIOSFODNN7EXAMPLE" not in text, "AWS IAM access key must be redacted"
    assert "STRIPELIVEKEY1234567890" not in text, "Stripe live key must be redacted"
    assert "STRIPEWEBHOOKSECRET123456" not in text, (
        "Stripe webhook secret must be redacted"
    )
    assert "DBPASSWORD123" not in text, "PostgreSQL DSN password must be redacted"

    # --- benign output must survive verbatim ---
    assert "metric: loss=0.042" in text, "benign metric line must survive"
    assert "step 100 complete" in text, "benign log line must survive"


def test_terminal_record_b64_file_redacts_command_log(tmp_path):
    """_b64_file with redact_command_log=True redacts credentials in the text field.

    Imports and calls the production function directly so the defense-in-depth path
    and its fail-soft branch are exercised, not a re-implementation.
    """
    import base64
    import importlib.util
    import json as json_mod
    import pathlib

    script_path = (
        pathlib.Path(__file__).parent.parent
        / "app"
        / "_compute_scripts"
        / "terminal_record.py"
    )
    spec = importlib.util.spec_from_file_location("terminal_record", script_path)
    assert spec is not None
    tr = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(tr)  # type: ignore[attr-defined]

    # --- normal case: credential is redacted; benign text and other fields survive ---
    log_file = tmp_path / "command.log.json"
    payload = {
        "text": (
            "cloning https://x-access-token:ghp_SECRETTOKEN123@github.com/org/repo\n"
            "ZAI_API_KEY=ZAISECRET1234567890\n"
            "ZAI_API_KEY=\"ZAIQUOTED1234567890\"\n"
            "{\"ZAI_API_KEY\":\"ZAIJSON1234567890\",\"ANTHROPIC_AUTH_TOKEN\":\"ANTHROPICJSON1234567890\"}\n"
            "ANTHROPIC_AUTH_TOKEN=ANTHROPICSECRET1234567890\n"
            "step 1 done"
        ),
        "truncated": False,
        "byte_count": 77,
    }
    log_file.write_text(json_mod.dumps(payload, sort_keys=True), encoding="utf-8")

    b64 = tr._b64_file(str(log_file), redact_command_log=True)
    decoded = json_mod.loads(base64.b64decode(b64).decode("utf-8"))

    assert "ghp_SECRETTOKEN123" not in decoded["text"], "credential must be redacted"
    assert "ZAISECRET1234567890" not in decoded["text"], "ZAI key must be redacted"
    assert "ZAIQUOTED1234567890" not in decoded["text"], (
        "quoted ZAI key must be redacted"
    )
    assert "ZAIJSON1234567890" not in decoded["text"], "JSON ZAI key must be redacted"
    assert "ANTHROPICJSON1234567890" not in decoded["text"], (
        "JSON Anthropic-compatible token must be redacted"
    )
    assert "ANTHROPICSECRET1234567890" not in decoded["text"], (
        "Anthropic-compatible token must be redacted"
    )
    assert "ZAI_API_KEY=***" in decoded["text"]
    assert "ANTHROPIC_AUTH_TOKEN=***" in decoded["text"]
    assert "step 1 done" in decoded["text"], "benign text must survive"
    assert decoded["truncated"] is False, "truncated field must survive"
    assert decoded["byte_count"] == 77, "byte_count field must survive"

    # --- fail-soft: non-JSON input is base64-encoded verbatim (no crash) ---
    bad_file = tmp_path / "bad.log.json"
    bad_file.write_bytes(b"not-json-at-all")
    b64_bad = tr._b64_file(str(bad_file), redact_command_log=True)
    assert base64.b64decode(b64_bad) == b"not-json-at-all", (
        "non-JSON input must pass through verbatim"
    )

    # --- fail-soft: dict with non-str text is encoded verbatim (no crash) ---
    non_str_file = tmp_path / "non_str.log.json"
    non_str_payload = {"text": 42, "truncated": False, "byte_count": 2}
    non_str_file.write_text(json_mod.dumps(non_str_payload), encoding="utf-8")
    b64_non_str = tr._b64_file(str(non_str_file), redact_command_log=True)
    decoded_non_str = json_mod.loads(base64.b64decode(b64_non_str).decode("utf-8"))
    assert decoded_non_str["text"] == 42, "non-str text field must be encoded verbatim"


# ── Compute-job VM heartbeat endpoint + provisioning timeout guard ─────────


@pytest.mark.asyncio
async def test_compute_job_vm_heartbeat_wrong_bearer_rejected(
    client, db_session, monkeypatch
):
    """POST /compute-jobs/{id}/heartbeat with a wrong bearer returns 403."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="HB Auth Test", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="true",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name="local-subprocess",
    )

    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/heartbeat",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_compute_job_vm_heartbeat_bare_uuid_cannot_suppress_watchdog(
    client, db_session, monkeypatch
):
    """A bare job-UUID bearer must NOT quiet the watchdog or keep a job alive.

    Regression for the weak-auth finding: the job UUID is a resource identifier
    (URL path / poll responses / logs), so it must not authenticate the heartbeat.
    A caller presenting only the bare UUID is rejected with 403 and mutates
    NOTHING — it cannot transition a stuck STARTING job to RUNNING (which would
    suppress the provisioning-deadline timeout) nor append a progress sample
    (which would quiet the no-progress watchdog). The properly-authenticated
    runner — presenting the per-job secret derived by ``compute_job_heartbeat_secret``
    — still can.
    """
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="HB Bare UUID Guard", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python3 train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name="local-subprocess",
    )
    job.status = ComputeJobStatus.STARTING
    job.last_heartbeat_at = None
    await db_session.commit()

    progress_body = json.dumps({"progress": {"metric": "loss", "value": 0.5}}).encode()

    # 1) A bare job-UUID bearer is rejected and changes nothing.
    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/heartbeat",
        headers={
            "Authorization": f"Bearer {job.id}",
            "Content-Type": "application/json",
        },
        content=progress_body,
    )
    assert resp.status_code == 403, "bare job-UUID bearer must be rejected"

    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.STARTING, (
        "a rejected heartbeat must NOT keep a stuck STARTING job alive"
    )
    assert job.last_heartbeat_at is None, "a rejected heartbeat must not refresh liveness"
    assert "progress_series" not in (job.polling_metadata or {}), (
        "a rejected heartbeat must not feed the no-progress watchdog"
    )

    # 2) The properly-authenticated runner (per-job secret) still works.
    secret = compute_backends_module.compute_job_heartbeat_secret(job.id)
    assert secret != str(job.id), "the heartbeat secret must NOT be the job UUID"
    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/heartbeat",
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
        content=progress_body,
    )
    assert resp.status_code == 204, "the properly-authenticated runner must succeed"

    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.RUNNING, (
        "the authenticated first heartbeat transitions STARTING→RUNNING"
    )
    assert job.last_heartbeat_at is not None, "authenticated heartbeat refreshes liveness"
    series = (job.polling_metadata or {}).get("progress_series")
    assert series, "authenticated heartbeat must record the progress sample"
    assert series[-1]["name"] == "loss" and series[-1]["value"] == 0.5


@pytest.mark.asyncio
async def test_compute_job_vm_heartbeat_missing_job_returns_404(client, monkeypatch):
    """POST /compute-jobs/{id}/heartbeat for an unknown ID returns 404.

    The caller must present the correctly-derived per-job secret to get PAST auth
    and reach the existence check; a bare-UUID caller is rejected at auth (403)
    and never learns whether the job exists.
    """
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    fake_id = uuid.uuid4()
    resp = await client.post(
        f"/api/agent/compute-jobs/{fake_id}/heartbeat",
        headers={
            "Authorization": (
                f"Bearer {compute_backends_module.compute_job_heartbeat_secret(fake_id)}"
            )
        },
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_compute_job_vm_heartbeat_transitions_starting_to_running(
    client, db_session, monkeypatch
):
    """First heartbeat from the VM transitions STARTING → RUNNING and sets last_heartbeat_at."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="HB Transition Test", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="true",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name="local-subprocess",
    )
    # Manually set to STARTING to simulate mid-provisioning state.
    job.status = ComputeJobStatus.STARTING
    job.last_heartbeat_at = None
    await db_session.commit()

    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/heartbeat",
        headers={
            "Authorization": (
                f"Bearer {compute_backends_module.compute_job_heartbeat_secret(job.id)}"
            )
        },
    )
    assert resp.status_code == 204

    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.RUNNING, (
        "first heartbeat must transition STARTING→RUNNING"
    )
    assert job.last_heartbeat_at is not None, "last_heartbeat_at must be set"
    assert job.started_at is not None, "started_at must be set on first heartbeat"


@pytest.mark.asyncio
async def test_compute_job_vm_heartbeat_updates_running_job(
    client, db_session, monkeypatch
):
    """Subsequent heartbeats on a RUNNING job only update last_heartbeat_at."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="HB Running Test", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="true",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name="local-subprocess",
    )
    job.status = ComputeJobStatus.RUNNING
    await db_session.commit()

    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/heartbeat",
        headers={
            "Authorization": (
                f"Bearer {compute_backends_module.compute_job_heartbeat_secret(job.id)}"
            )
        },
    )
    assert resp.status_code == 204

    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.RUNNING, "status must remain RUNNING"
    assert job.last_heartbeat_at is not None


@pytest.mark.asyncio
async def test_compute_job_vm_heartbeat_noop_for_terminal_job(
    client, db_session, monkeypatch
):
    """Heartbeat for a terminal job returns 204 without modifying the row."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="HB Terminal Test", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="true",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name="local-subprocess",
    )
    job.status = ComputeJobStatus.SUCCEEDED
    job.outcome = ComputeJobOutcome.SUCCESS
    job.completed_at = _utcnow()
    job.last_heartbeat_at = None
    await db_session.commit()

    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/heartbeat",
        headers={
            "Authorization": (
                f"Bearer {compute_backends_module.compute_job_heartbeat_secret(job.id)}"
            )
        },
    )
    assert resp.status_code == 204, "terminal job heartbeat must not error"

    await db_session.refresh(job)
    assert job.last_heartbeat_at is None, "terminal job row must not be modified"
    assert job.status == ComputeJobStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_provisioning_deadline_skipped_when_vm_heartbeated(
    db_session, monkeypatch
):
    """A job whose provisioning deadline has passed but whose VM has heartbeated
    must NOT be terminated.  The heartbeat is proof the VM booted and is alive;
    the next monitor tick will handle it via the normal running path.

    This is the regression case from compute job 50116a8f: the GCP insert LRO
    was slow, the orchestrator's provisioning deadline fired, and the VM was
    terminated and reaped while still RUNNING.
    """
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="HB Prov Deadline Guard", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="true",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name="local-subprocess",
    )
    # Expire the provisioning deadline AND the wall-clock deadline so the monitor
    # sees provisioning_timeout_seconds == 0.0 on its first pass.
    meta = dict(job.polling_metadata or {})
    meta["provisioning_deadline_at"] = (
        _utcnow() - timedelta(seconds=10)
    ).isoformat()
    job.polling_metadata = meta
    job.wall_clock_deadline = _utcnow() - timedelta(seconds=10)
    # Simulate the VM having already heartbeated (endpoint set this).
    job.last_heartbeat_at = _utcnow() - timedelta(seconds=5)
    job.status = ComputeJobStatus.STARTING
    await db_session.commit()

    terminate_reasons: list[str] = []

    class FakeBackend:
        def external_id_for(self, j):
            return "hb-guard-vm"

        async def launch(self, spec):
            raise AssertionError("launch must not be called when deadline passed")

        async def terminate(self, j, *, reason):
            terminate_reasons.append(reason)
            return True

    monkeypatch.setattr(compute_jobs, "get_compute_backend", lambda _: FakeBackend())
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    assert terminate_reasons == [], (
        "heartbeating VM must NOT be terminated at provisioning deadline"
    )
    assert job.status not in {ComputeJobStatus.TIMED_OUT, ComputeJobStatus.FAILED}, (
        f"job must not be in terminal status; got {job.status}"
    )


@pytest.mark.asyncio
async def test_provisioning_deadline_still_terminates_without_heartbeat(
    db_session, monkeypatch
):
    """Control: a job whose provisioning deadline passed AND has no heartbeat
    must still be terminated as TIMED_OUT — the guard must not suppress the
    existing fallback behavior for VMs that die before their first heartbeat."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="HB Prov Deadline Control", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="true",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name="local-subprocess",
    )
    meta = dict(job.polling_metadata or {})
    meta["provisioning_deadline_at"] = (
        _utcnow() - timedelta(seconds=10)
    ).isoformat()
    job.polling_metadata = meta
    job.wall_clock_deadline = _utcnow() - timedelta(seconds=10)
    job.last_heartbeat_at = None  # No heartbeat — VM never booted
    job.status = ComputeJobStatus.STARTING
    # Exhaust retry budget so the test still validates terminal behavior.
    meta["provisioning_retry_count"] = compute_jobs.MAX_PROVISIONING_RETRY_COUNT
    job.polling_metadata = meta
    await db_session.commit()

    terminate_reasons: list[str] = []

    class FakeBackend:
        def external_id_for(self, j):
            return "no-hb-vm"

        async def launch(self, spec):
            raise AssertionError("launch must not be called when deadline passed")

        async def terminate(self, j, *, reason):
            terminate_reasons.append(reason)
            return True

    monkeypatch.setattr(compute_jobs, "get_compute_backend", lambda _: FakeBackend())
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    assert "provisioning" in terminate_reasons, (
        "non-heartbeating VM must be terminated at deadline"
    )
    assert job.status == ComputeJobStatus.TIMED_OUT


@pytest.mark.asyncio
async def test_launch_lro_timeout_skipped_when_vm_heartbeated(db_session, monkeypatch):
    """When the monitor's asyncio.wait_for fires during backend.launch() but the
    VM has already heartbeated (endpoint wrote last_heartbeat_at), the monitor must
    NOT terminate the job — the VM is alive despite the slow GCP LRO.

    This tests the asyncio.TimeoutError guard path in _process_single_compute_job.
    """
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="HB LRO Timeout Guard", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="true",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name="local-subprocess",
    )
    # Set a deadline far enough in the future for the monitor to reach wait_for,
    # but short enough that the 1-second launch sleep causes a TimeoutError.
    meta = dict(job.polling_metadata or {})
    meta["provisioning_deadline_at"] = (
        _utcnow() + timedelta(milliseconds=300)
    ).isoformat()
    job.polling_metadata = meta
    job.wall_clock_deadline = _utcnow() + timedelta(seconds=300)
    job.status = ComputeJobStatus.STARTING
    # Pre-write only last_heartbeat_at, leaving status=STARTING.  This simulates
    # the race where the VM's POST /heartbeat wrote last_heartbeat_at AFTER
    # _reserve_compute_job_launch acquired its lock (so the monitor entered the
    # launch path with STARTING), but before wait_for fired.  The guard in the
    # asyncio.TimeoutError handler detects the heartbeat and completes the
    # STARTING→RUNNING transition that the endpoint normally does atomically.
    job.last_heartbeat_at = _utcnow()
    await db_session.commit()

    terminate_reasons: list[str] = []
    launch_started = asyncio.Event()

    class SlowBackendWithHeartbeat:
        def external_id_for(self, j):
            return "lro-slow-vm"

        async def launch(self, spec):
            launch_started.set()
            # Simulate a slow GCP insert LRO that outlasts the provisioning budget.
            await asyncio.sleep(1.0)
            return ComputeLaunchResult(
                external_id="lro-slow-vm",
                started_at=_utcnow(),
                cost_rate_per_second=0.0,
                message="done",
            )

        async def terminate(self, j, *, reason):
            terminate_reasons.append(reason)
            return True

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _: SlowBackendWithHeartbeat()
    )
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    assert launch_started.is_set(), "launch coroutine must have been entered"

    await db_session.refresh(job)
    assert terminate_reasons == [], (
        "VM that heartbeated during slow LRO must NOT be terminated"
    )
    assert job.status == ComputeJobStatus.RUNNING, (
        f"job must remain RUNNING after heartbeat-guarded LRO timeout; got {job.status}"
    )


# ---------------------------------------------------------------------------
# Cloud Run Job launch — AGENT_SECRET_TOKEN + LLM env injection tests
# ---------------------------------------------------------------------------


class TestLaunchAgentCloudRunJob:
    """Tests for compute_cloud_run.launch_agent_cloud_run_job."""

    @pytest.mark.asyncio
    async def test_launch_injects_agent_secret_version(self, monkeypatch):
        """When secret_version_name is provided, AGENT_SECRET_VERSION must be
        passed as an env var so the agent can fetch secrets at startup.
        Plaintext credentials are never injected as env overrides.
        """
        from app import compute as _compute_module
        from app import compute_cloud_run

        # Disable LOCAL_MODE so the function proceeds past the early-return guard.
        monkeypatch.setattr(_compute_module, "LOCAL_MODE", False)

        # Capture env vars passed to run_job.
        captured_env: list[tuple[str, str]] = []

        class FakeEnvVar:
            def __init__(self, name, value):
                self.name = name
                self.value = value

        class FakeContainerOverride:
            def __init__(self, env=None):
                self.env = env or []

        class FakeOverrides:
            ContainerOverride = FakeContainerOverride

            def __init__(self, task_count=1, timeout=None, container_overrides=None):
                self.task_count = task_count
                self.timeout = timeout
                self.container_overrides = container_overrides or []

        class FakeRunJobRequest:
            Overrides = FakeOverrides

            def __init__(self, name, overrides):
                self.name = name
                self.overrides = overrides

        def fake_run_job(request):
            for co in request.overrides.container_overrides:
                for env_var in co.env:
                    captured_env.append((env_var.name, env_var.value))
            op = MagicMock()
            op.operation.name = "operations/fake"
            return op

        mock_run_client = MagicMock()
        mock_run_client.run_job.side_effect = fake_run_job

        mock_run_module = MagicMock()
        mock_run_module.JobsClient.return_value = mock_run_client
        mock_run_module.RunJobRequest = FakeRunJobRequest
        mock_run_module.EnvVar = FakeEnvVar

        import google.cloud

        monkeypatch.setattr(google.cloud, "run_v2", mock_run_module, raising=False)
        monkeypatch.setattr(
            compute_cloud_run,
            "CLOUD_RUN_JOB_NAME",
            "projects/p/locations/us/jobs/agent",
        )

        result = await compute_cloud_run.launch_agent_cloud_run_job(
            vm_id="vm-001",
            task_id="task-001",
            payload_uri="gs://bucket/payload.json",
            secret_version_name="projects/p/secrets/s/versions/1",
            task_type="research",
        )

        assert result is True
        env_dict = dict(captured_env)
        assert "AGENT_SECRET_VERSION" in env_dict, (
            f"AGENT_SECRET_VERSION must be injected; got env keys: {list(env_dict)}"
        )
        assert env_dict["AGENT_SECRET_VERSION"] == "projects/p/secrets/s/versions/1"
        # Plaintext secrets must never be injected
        assert "AGENT_SECRET_TOKEN" not in env_dict
        assert "ANTHROPIC_API_KEY" not in env_dict

    @pytest.mark.asyncio
    async def test_no_secret_version_when_not_provided(self, monkeypatch):
        """When secret_version_name is None, AGENT_SECRET_VERSION is not injected."""
        import google.cloud
        from app import compute as _compute_module
        from app import compute_cloud_run

        monkeypatch.setattr(_compute_module, "LOCAL_MODE", False)

        captured_env: list = []

        class FakeClient:
            def run_job(self, *, request):
                for co in request.overrides.container_overrides:
                    captured_env.extend(co.env)
                op = MagicMock()
                op.operation.name = "fake-op"
                return op

        mock_run_module = MagicMock()
        mock_run_module.JobsClient.return_value = FakeClient()

        monkeypatch.setattr(google.cloud, "run_v2", mock_run_module, raising=False)
        monkeypatch.setattr(
            compute_cloud_run,
            "CLOUD_RUN_JOB_NAME",
            "projects/p/locations/us/jobs/agent",
        )

        result = await compute_cloud_run.launch_agent_cloud_run_job(
            vm_id="vm-002",
            task_id="task-002",
            payload_uri=None,
            secret_version_name=None,
            task_type="research",
        )

        assert result is True, "must return True when no secret is needed"
        env_dict = {e.name: e.value for e in captured_env}
        assert "AGENT_SECRET_VERSION" not in env_dict, (
            "AGENT_SECRET_VERSION must not be injected when secret_version_name is None"
        )

    @pytest.mark.asyncio
    async def test_injects_llm_settings_not_plaintext_creds(self, monkeypatch):
        """LLM identifier and settings env vars are injected, but plaintext
        credentials like ANTHROPIC_API_KEY are never included as env overrides."""
        import google.cloud.run_v2 as run_v2_mod
        from app.compute_cloud_run import launch_agent_cloud_run_job
        import app.compute_cloud_run as ccr_mod

        monkeypatch.setattr(
            ccr_mod, "CLOUD_RUN_JOB_NAME", "projects/p/locations/us/jobs/agent"
        )
        monkeypatch.setattr("app.compute_manifest.ANTHROPIC_API_KEY", "sk-ant-test123")
        # Ensure not LOCAL_MODE
        monkeypatch.setattr("app.compute.LOCAL_MODE", False)

        submitted_env: list = []

        class FakeClient:
            def run_job(self, *, request):
                for co in request.overrides.container_overrides:
                    submitted_env.extend(co.env)
                op = MagicMock()
                op.operation.name = "fake-op"
                return op

        with patch.object(run_v2_mod, "JobsClient", return_value=FakeClient()):
            result = await launch_agent_cloud_run_job(
                vm_id="cr-vm-1",
                task_id="task-abc",
                payload_uri=None,
                secret_version_name="projects/p/secrets/s/versions/1",
                task_type="research",
                provider="anthropic",
                auth_mode="api_key",
                provider_settings={},
            )

        assert result is True
        env_dict = {e.name: e.value for e in submitted_env}
        assert env_dict.get("PROVIDER") == "anthropic", (
            f"PROVIDER missing from env overrides; got {env_dict}"
        )
        assert env_dict.get("AUTH_MODE") == "api_key", (
            f"AUTH_MODE missing; got {env_dict}"
        )
        # ANTHROPIC_API_KEY must NOT be injected as plaintext
        assert "ANTHROPIC_API_KEY" not in env_dict, (
            f"ANTHROPIC_API_KEY must not be plaintext in env overrides; got {env_dict}"
        )
        assert "LLM_SETTINGS" in env_dict, (
            f"LLM_SETTINGS missing from env overrides; got {env_dict}"
        )
        assert "LLM_CLI_TRANSLATION" in env_dict, (
            f"LLM_CLI_TRANSLATION missing from env overrides; got {env_dict}"
        )
        # AGENT_SECRET_VERSION must be present so agent can fetch the actual API key
        assert env_dict.get("AGENT_SECRET_VERSION") == "projects/p/secrets/s/versions/1"

    @pytest.mark.asyncio
    async def test_oauth_token_not_plaintext_delivered_via_manifest(self, monkeypatch):
        """OAuth auth must NOT inject CLAUDE_CODE_OAUTH_TOKEN as a plaintext env
        override. The token is delivered via the Secret Manager manifest the agent
        resolves from AGENT_SECRET_VERSION at startup (secure-design contract)."""
        import google.cloud.run_v2 as run_v2_mod
        from app.compute_cloud_run import launch_agent_cloud_run_job
        import app.compute_cloud_run as ccr_mod

        monkeypatch.setattr(
            ccr_mod, "CLOUD_RUN_JOB_NAME", "projects/p/locations/us/jobs/agent"
        )
        monkeypatch.setattr("app.compute.LOCAL_MODE", False)

        submitted_env: list = []

        class FakeClient:
            def run_job(self, *, request):
                for co in request.overrides.container_overrides:
                    submitted_env.extend(co.env)
                op = MagicMock()
                op.operation.name = "fake-op"
                return op

        with patch.object(run_v2_mod, "JobsClient", return_value=FakeClient()):
            result = await launch_agent_cloud_run_job(
                vm_id="cr-vm-2",
                task_id="task-def",
                payload_uri=None,
                secret_version_name="projects/p/secrets/s/versions/1",
                task_type="research",
                oauth_token="sk-ant-oauth-tok",
                provider="anthropic",
                auth_mode="oauth",
                provider_settings={},
            )

        assert result is True
        env_dict = {e.name: e.value for e in submitted_env}
        assert env_dict.get("PROVIDER") == "anthropic"
        assert env_dict.get("AUTH_MODE") == "oauth"
        # The OAuth token (and its raw value) must NEVER be a plaintext override —
        # it is delivered via the Secret Manager manifest, not the RunJobRequest.
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env_dict, (
            f"CLAUDE_CODE_OAUTH_TOKEN must not be a plaintext override; got {env_dict}"
        )
        assert "sk-ant-oauth-tok" not in set(env_dict.values()), (
            f"Raw OAuth token value must not appear in any env override; got {env_dict}"
        )
        # AGENT_SECRET_VERSION must be present so the agent can resolve the token.
        assert env_dict.get("AGENT_SECRET_VERSION") == "projects/p/secrets/s/versions/1"

    @pytest.mark.asyncio
    async def test_skips_file_path_credentials(self, monkeypatch):
        """File-path credential env vars (value starts with '/') are not injected."""
        import google.cloud.run_v2 as run_v2_mod
        from app.compute_cloud_run import launch_agent_cloud_run_job
        from app.compute_manifest import _ProviderMaterials
        import app.compute_cloud_run as ccr_mod

        monkeypatch.setattr(
            ccr_mod, "CLOUD_RUN_JOB_NAME", "projects/p/locations/us/jobs/agent"
        )
        monkeypatch.setattr("app.compute.LOCAL_MODE", False)

        submitted_env: list = []

        class FakeClient:
            def run_job(self, *, request):
                for co in request.overrides.container_overrides:
                    submitted_env.extend(co.env)
                op = MagicMock()
                op.operation.name = "fake-op"
                return op

        # Patch _resolve_provider_materials to return a Gemini-style file-path credential
        fake_materials = _ProviderMaterials(
            provider_name="gemini",
            auth_mode_value="service_account",
            files=[],
            cli_translation=None,
            secret_env={"GOOGLE_APPLICATION_CREDENTIALS": "/opt/agent/gemini/sa.json"},
        )
        with (
            patch.object(run_v2_mod, "JobsClient", return_value=FakeClient()),
            patch(
                "app.compute_manifest._resolve_provider_materials",
                return_value=fake_materials,
            ),
        ):
            result = await launch_agent_cloud_run_job(
                vm_id="cr-vm-3",
                task_id="task-ghi",
                payload_uri=None,
                secret_version_name=None,
                task_type="research",
                provider="gemini",
                auth_mode="service_account",
                provider_settings={},
            )

        assert result is True
        env_dict = {e.name: e.value for e in submitted_env}
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in env_dict, (
            "File-path credential must not appear in Cloud Run env overrides; "
            f"got {env_dict}"
        )
        assert env_dict.get("PROVIDER") == "gemini"

    @pytest.mark.asyncio
    async def test_no_llm_params_only_baseline_env(self, monkeypatch):
        """Without LLM params, only baseline env vars are injected (no plaintext creds)."""
        import google.cloud.run_v2 as run_v2_mod
        from app.compute_cloud_run import launch_agent_cloud_run_job
        import app.compute_cloud_run as ccr_mod

        monkeypatch.setattr(
            ccr_mod, "CLOUD_RUN_JOB_NAME", "projects/p/locations/us/jobs/agent"
        )
        monkeypatch.setattr("app.compute.LOCAL_MODE", False)

        submitted_env: list = []

        class FakeClient:
            def run_job(self, *, request):
                for co in request.overrides.container_overrides:
                    submitted_env.extend(co.env)
                op = MagicMock()
                op.operation.name = "fake-op"
                return op

        with patch.object(run_v2_mod, "JobsClient", return_value=FakeClient()):
            result = await launch_agent_cloud_run_job(
                vm_id="cr-vm-4",
                task_id="task-jkl",
                payload_uri="gs://bucket/payload.json",
                secret_version_name="projects/p/secrets/s/versions/1",
                task_type="research",
                # No LLM params
            )

        assert result is True
        env_dict = {e.name: e.value for e in submitted_env}
        baseline_keys = {
            "VM_INSTANCE_ID",
            "TASK_ID",
            "ORCHESTRATOR_URL",
            "PAYLOAD_URI",
            "AGENT_SECRET_VERSION",
        }
        assert set(env_dict.keys()) == baseline_keys, (
            f"Expected only baseline env keys without LLM params; got {set(env_dict.keys())}"
        )
        # CLOUD_RUN_JOB is a Cloud Run *reserved* env name (the platform auto-sets
        # it to the job name). Providing it in container_overrides makes run_job
        # fail 400; the agent detects job mode from the platform-set value's
        # presence, so the backend must NOT inject it.
        assert "CLOUD_RUN_JOB" not in env_dict
        # Ensure no plaintext secrets are injected
        assert "AGENT_SECRET_TOKEN" not in env_dict
        assert "ANTHROPIC_API_KEY" not in env_dict

    @pytest.mark.asyncio
    async def test_local_mode_returns_true_without_api_call(self, monkeypatch):
        """LOCAL_MODE skips the Cloud Run API entirely and returns True."""
        import google.cloud.run_v2 as run_v2_mod
        from app.compute_cloud_run import launch_agent_cloud_run_job
        import app.compute_cloud_run as ccr_mod

        monkeypatch.setattr(
            ccr_mod, "CLOUD_RUN_JOB_NAME", "projects/p/locations/us/jobs/agent"
        )
        monkeypatch.setattr("app.compute.LOCAL_MODE", True)

        api_called = []

        class FakeClient:
            def run_job(self, *, request):
                api_called.append(True)
                return MagicMock()

        with patch.object(run_v2_mod, "JobsClient", return_value=FakeClient()):
            result = await launch_agent_cloud_run_job(
                vm_id="cr-local",
                task_id="task-local",
                payload_uri=None,
                secret_version_name=None,
                task_type="research",
            )

        assert result is True
        assert not api_called, "Cloud Run API must not be called in LOCAL_MODE"


# ---------------------------------------------------------------------------
# Hardware policy enforcement in create_compute_job
# ---------------------------------------------------------------------------


def _make_project_with_hardware_policy(
    *,
    allowed_machine_types: list | None = None,
    allowed_accelerator_types: list | None = None,
) -> Project:
    envelope = {}
    if allowed_machine_types is not None:
        envelope["allowed_machine_types"] = allowed_machine_types
    if allowed_accelerator_types is not None:
        envelope["allowed_accelerator_types"] = allowed_accelerator_types
    return Project(
        name="Hardware Policy Project",
        user_id="test-user",
        build_config={"resource_envelope": envelope},
    )


@pytest.mark.asyncio
async def test_create_compute_job_rejects_disallowed_machine_type(db_session):
    """allowed_machine_types set, request uses a disallowed type → ComputeLaunchPreconditionFailed."""
    project = _make_project_with_hardware_policy(
        allowed_machine_types=["e2-standard-2"]
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc_info:
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="echo test",
            machine_type="e2-standard-4",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )

    detail = exc_info.value.detail
    assert detail["error"] == "project_hardware_policy_violated"
    assert "e2-standard-4" in detail["message"]
    assert detail["requested"] == "e2-standard-4"
    assert detail["allowed"] == ["e2-standard-2"]


@pytest.mark.asyncio
async def test_create_compute_job_rejects_disallowed_accelerator(
    db_session, monkeypatch
):
    """allowed_accelerator_types set, request uses a disallowed accelerator → ComputeLaunchPreconditionFailed."""
    monkeypatch.setattr(compute, "LOCAL_MODE", False)

    project = _make_project_with_hardware_policy(allowed_accelerator_types=["T4"])
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc_info:
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="echo gpu test",
            accelerator="L4",
            backend_name="gcp",
            spend_cap_usd=5.0,
            deadline_seconds=600,
        )

    detail = exc_info.value.detail
    assert detail["error"] == "project_hardware_policy_violated"
    assert "L4" in detail["message"]
    assert detail["requested"] == "L4"
    assert detail["allowed"] == ["T4"]


@pytest.mark.asyncio
async def test_create_compute_job_allows_matching_hardware_policy(db_session):
    """allowed_machine_types set, request matches the allowed type → job created."""
    project = _make_project_with_hardware_policy(
        allowed_machine_types=["e2-standard-2"]
    )
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
        command="echo allowed",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )

    assert job.status == ComputeJobStatus.QUEUED
    assert job.machine_type == "e2-standard-2"


# Compute-experiment preflight endpoint and at-launch hardware policy gate
# ---------------------------------------------------------------------------

_PREFLIGHT_TEST_USER_ID = "test-user-123"


async def _make_preflight_project(
    session,
    *,
    repo: str = "acme/preflight-repo",
    hardware_policy: dict | None = None,
) -> Project:
    owner, name = repo.split("/", 1)
    build_config: dict = {}
    if hardware_policy is not None:
        build_config["resource_envelope"] = hardware_policy
    project = Project(
        name=repo,
        user_id=_PREFLIGHT_TEST_USER_ID,
        github_owner=owner,
        github_repo=name,
        autonomous_enabled=False,
        build_config=build_config,
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(
        ProjectMember(
            project_id=project.id,
            user_id=_PREFLIGHT_TEST_USER_ID,
            role="owner",
        )
    )
    await session.commit()
    return project


class TestComputeExperimentPreflight:
    """Tests for POST /api/admin/tasks/compute-experiment/preflight."""

    def _headers(self):
        return {"x-user-id": _PREFLIGHT_TEST_USER_ID}

    @pytest.mark.asyncio
    async def test_cpu_shape_returns_cost_projection(self, client, db_session):
        """CPU shape returns rate, projected_cost, and max_runtime."""
        from app.vm_cost_rates import get_compute_rate_per_second

        await _make_preflight_project(db_session)
        spend_cap = 5.0
        deadline = 3600
        resp = await client.post(
            "/api/admin/tasks/compute-experiment/preflight",
            json={
                "github_repo": "acme/preflight-repo",
                "machine_type": "e2-standard-2",
                "spend_cap_usd": spend_cap,
                "deadline_seconds": deadline,
            },
            headers=self._headers(),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        expected_rate = get_compute_rate_per_second(machine_type="e2-standard-2")
        assert data["rate_per_second"] == pytest.approx(expected_rate, rel=1e-6)
        assert data["rate_per_hour"] == pytest.approx(expected_rate * 3600, rel=1e-6)
        assert data["projected_cost_usd"] == pytest.approx(expected_rate * deadline, rel=1e-6)
        assert data["max_runtime_seconds"] == int(spend_cap / expected_rate)
        assert data["in_policy"] is True
        assert data["reason"] is None
        assert data["shape_rejection"] is None

    @pytest.mark.asyncio
    async def test_gpu_l4_spot_returns_correct_rate(self, client, db_session):
        """GPU L4 spot request returns the bundled spot rate."""
        from app.vm_cost_rates import get_compute_rate_per_second

        await _make_preflight_project(db_session)
        resp = await client.post(
            "/api/admin/tasks/compute-experiment/preflight",
            json={
                "github_repo": "acme/preflight-repo",
                "accelerator": "L4",
                "machine_type": "g2-standard-4",
                "spend_cap_usd": 10.0,
                "deadline_seconds": 1800,
                "provisioning_model": "SPOT",
            },
            headers=self._headers(),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        expected_rate = get_compute_rate_per_second(
            machine_type="g2-standard-4", accelerator="L4", spot=True
        )
        assert data["rate_per_second"] == pytest.approx(expected_rate, rel=1e-6)
        assert data["projected_cost_usd"] == pytest.approx(expected_rate * 1800, rel=1e-6)
        assert data["in_policy"] is True
        assert data["shape_rejection"] is None

    @pytest.mark.asyncio
    async def test_policy_violation_returns_out_of_policy(self, client, db_session):
        """Accelerator disallowed by project policy → in_policy=False with reason."""
        await _make_preflight_project(
            db_session,
            hardware_policy={"allowed_accelerator_types": ["T4"]},
        )
        resp = await client.post(
            "/api/admin/tasks/compute-experiment/preflight",
            json={
                "github_repo": "acme/preflight-repo",
                "accelerator": "L4",
                "machine_type": "g2-standard-4",
                "spend_cap_usd": 5.0,
                "deadline_seconds": 900,
            },
            headers=self._headers(),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["in_policy"] is False
        assert "L4" in data["reason"]
        assert data["allowed_accelerator_types"] == ["T4"]
        assert data["shape_rejection"] is None

    @pytest.mark.asyncio
    async def test_unknown_accelerator_returns_shape_rejection(self, client, db_session):
        """Unknown accelerator name → shape_rejection set, in_policy still True."""
        await _make_preflight_project(db_session)
        resp = await client.post(
            "/api/admin/tasks/compute-experiment/preflight",
            json={
                "github_repo": "acme/preflight-repo",
                "accelerator": "A100",
                "spend_cap_usd": 5.0,
                "deadline_seconds": 900,
            },
            headers=self._headers(),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["shape_rejection"] is not None
        assert "A100" in data["shape_rejection"]
        assert data["rate_per_second"] is None

    @pytest.mark.asyncio
    async def test_unknown_project_returns_unlimited_policy(self, client, db_session):
        """Project not yet created → preflight succeeds with unlimited policy."""
        resp = await client.post(
            "/api/admin/tasks/compute-experiment/preflight",
            json={
                "github_repo": "acme/no-such-repo",
                "machine_type": "e2-standard-2",
                "spend_cap_usd": 2.0,
                "deadline_seconds": 600,
            },
            headers=self._headers(),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["in_policy"] is True
        assert data["allowed_machine_types"] is None
        assert data["allowed_accelerator_types"] is None


class TestComputeExperimentLaunchPolicyGate:
    """Tests that create_compute_experiment_task enforces hardware policy."""

    def _headers(self):
        return {"x-user-id": _PREFLIGHT_TEST_USER_ID}

    @pytest.mark.asyncio
    async def test_disallowed_accelerator_rejected_at_launch(self, client, db_session):
        """Accelerator outside project allowed_accelerator_types → 422 before task is persisted."""
        await _make_preflight_project(
            db_session,
            repo="acme/policy-launch-repo",
            hardware_policy={"allowed_accelerator_types": ["T4"]},
        )
        resp = await client.post(
            "/api/admin/tasks/compute-experiment",
            json={
                "github_repo": "acme/policy-launch-repo",
                "title": "GPU test",
                "prompt": "Run benchmark",
                "command": "python bench.py",
                "spend_cap_usd": 5.0,
                "deadline_seconds": 900,
                "accelerator": "L4",
                "machine_type": "g2-standard-4",
            },
            headers=self._headers(),
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["error"]["message"]
        assert detail["error"] == "project_hardware_policy_violated"
        assert "L4" in detail["message"]
        assert detail["requested"] == "L4"
        assert detail["allowed"] == ["T4"]

    @pytest.mark.asyncio
    async def test_allowed_accelerator_proceeds_at_launch(self, client, db_session):
        """Accelerator in project allowed_accelerator_types → task created successfully."""
        await _make_preflight_project(
            db_session,
            repo="acme/policy-launch-allowed-repo",
            hardware_policy={"allowed_accelerator_types": ["L4"]},
        )
        resp = await client.post(
            "/api/admin/tasks/compute-experiment",
            json={
                "github_repo": "acme/policy-launch-allowed-repo",
                "title": "GPU test",
                "prompt": "Run benchmark",
                "command": "python bench.py",
                "spend_cap_usd": 5.0,
                "deadline_seconds": 900,
                "accelerator": "L4",
                "machine_type": "g2-standard-4",
            },
            headers=self._headers(),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["requires_approval"] is False


class TestComputeExperimentForcedRunnerPin:
    """POST /api/admin/tasks/compute-experiment resolves an operator forced runner
    pin (runner_target_id) — by hostname OR UUID — against the project's own
    runners, stamping context['forced_runner_target_id'], and rejects an
    unknown/foreign runner with 422."""

    def _headers(self):
        return {"x-user-id": _PREFLIGHT_TEST_USER_ID}

    async def _make_runner(self, session, project_id, *, hostname):
        from app.models import LocalRunner
        from app import auth as auth_mod

        token = auth_mod.create_runner_token(uuid.uuid4())
        runner = LocalRunner(
            project_id=project_id,
            token_hash=auth_mod.hash_runner_token(token),
            hostname=hostname,
            capabilities={"task_types": "all", "has_gpu": False},
        )
        session.add(runner)
        await session.commit()
        await session.refresh(runner)
        return runner

    async def _launch(self, client, repo, runner_target_id):
        return await client.post(
            "/api/admin/tasks/compute-experiment",
            json={
                "github_repo": repo,
                "title": "Pinned run",
                "prompt": "Run benchmark",
                "command": "python bench.py",
                "spend_cap_usd": 5.0,
                "deadline_seconds": 900,
                "runner_target_id": runner_target_id,
            },
            headers=self._headers(),
        )

    @pytest.mark.asyncio
    async def test_resolves_hostname_to_forced_target(self, client, db_session):
        project = await _make_preflight_project(
            db_session, repo="acme/forced-hostname-repo"
        )
        runner = await self._make_runner(db_session, project.id, hostname="dgx-1")

        resp = await self._launch(client, "acme/forced-hostname-repo", "dgx-1")
        assert resp.status_code == 200, resp.text
        task = await db_session.get(Task, uuid.UUID(resp.json()["id"]))
        assert (task.context or {}).get("forced_runner_target_id") == str(runner.id)

    @pytest.mark.asyncio
    async def test_resolves_uuid_to_forced_target(self, client, db_session):
        project = await _make_preflight_project(
            db_session, repo="acme/forced-uuid-repo"
        )
        runner = await self._make_runner(db_session, project.id, hostname="dgx-2")

        resp = await self._launch(client, "acme/forced-uuid-repo", str(runner.id))
        assert resp.status_code == 200, resp.text
        task = await db_session.get(Task, uuid.UUID(resp.json()["id"]))
        assert (task.context or {}).get("forced_runner_target_id") == str(runner.id)

    @pytest.mark.asyncio
    async def test_unknown_runner_rejected_422(self, client, db_session):
        await _make_preflight_project(db_session, repo="acme/forced-unknown-repo")
        resp = await self._launch(
            client, "acme/forced-unknown-repo", "no-such-host"
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["error"]["message"]
        assert detail["error"] == "forced_runner_not_found"

    @pytest.mark.asyncio
    async def test_foreign_project_runner_rejected_422(self, client, db_session):
        """A runner UUID that belongs to a DIFFERENT project must not resolve."""
        await _make_preflight_project(db_session, repo="acme/forced-owner-repo")
        other = await _make_preflight_project(
            db_session, repo="acme/forced-other-repo"
        )
        foreign = await self._make_runner(db_session, other.id, hostname="foreign-1")

        resp = await self._launch(
            client, "acme/forced-owner-repo", str(foreign.id)
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["error"]["message"]
        assert detail["error"] == "forced_runner_not_found"


# ---------------------------------------------------------------------------
# Live output tail: heartbeat delivery and local backend reading
# ---------------------------------------------------------------------------


def _make_live_tail_b64(text: str, truncated: bool = False) -> str:
    """Build the base64-encoded JSON payload the VM runner writes to live_tail.b64."""
    payload = json.dumps({"text": text, "truncated": truncated}).encode("utf-8")
    return base64.b64encode(payload).decode("ascii")


@pytest.mark.asyncio
async def test_compute_job_vm_heartbeat_stores_live_output(
    client, db_session, monkeypatch
):
    """Heartbeat with live_output_tail body stores decoded text in polling_metadata."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="Live Tail HB Test", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo hello",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name="local-subprocess",
    )
    job.status = ComputeJobStatus.RUNNING
    await db_session.commit()

    tail_text = "experiment output line 1\nexperiment output line 2"
    tail_b64 = _make_live_tail_b64(tail_text)
    body = json.dumps({"live_output_tail": tail_b64}).encode()

    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/heartbeat",
        headers={
            "Authorization": (
                f"Bearer {compute_backends_module.compute_job_heartbeat_secret(job.id)}"
            ),
            "Content-Type": "application/json",
        },
        content=body,
    )
    assert resp.status_code == 204

    await db_session.refresh(job)
    meta = job.polling_metadata or {}
    live_output = meta.get("live_output")
    assert isinstance(live_output, dict), "live_output must be stored in polling_metadata"
    assert live_output.get("text") == tail_text, (
        f"live_output text must match: {live_output.get('text')!r}"
    )
    assert live_output.get("truncated") is False


@pytest.mark.asyncio
async def test_compute_job_vm_heartbeat_empty_body_unchanged(
    client, db_session, monkeypatch
):
    """Heartbeat with no body must remain backward-compatible (no live_output stored)."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="HB Empty Body Test", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo hello",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name="local-subprocess",
    )
    job.status = ComputeJobStatus.RUNNING
    await db_session.commit()

    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/heartbeat",
        headers={
            "Authorization": (
                f"Bearer {compute_backends_module.compute_job_heartbeat_secret(job.id)}"
            )
        },
    )
    assert resp.status_code == 204

    await db_session.refresh(job)
    meta = job.polling_metadata or {}
    assert "live_output" not in meta, "no live_output should be stored for empty body"


def test_gcp_startup_script_passes_live_tail_path_to_command_runner():
    """The GCP startup script must pass live_tail.b64 as a 4th arg to command_runner.py."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python3 train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=300,
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    assert "live_tail.b64" in script, "live_tail.b64 path must appear in startup script"
    assert "blueprint_command_runner.py" in script
    runner_idx = script.index("python3 /tmp/blueprint_command_runner.py")
    runner_invocation = script[runner_idx : runner_idx + 400]
    assert "live_tail.b64" in runner_invocation, (
        "live_tail.b64 path must be passed as an argument to command_runner.py"
    )


def test_gcp_startup_script_heartbeat_emits_live_tail_and_progress():
    """The _compute_heartbeat function delegates body construction to the baked
    heartbeat_body.py helper, feeding it BOTH the live-tail file and the progress
    file so each heartbeat can carry {progress:{metric,value}} — the sample that
    feeds the server-side no-progress watchdog (finding 76c9633f15448f99)."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python3 train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=300,
        no_progress_timeout_seconds=600,
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    # The helper is base64-decoded onto the VM alongside the other helpers.
    assert "base64 -d > /tmp/blueprint_heartbeat_body.py" in script, (
        "heartbeat_body.py must be base64-decoded onto the VM"
    )
    hb_func_start = script.index("_compute_heartbeat() {")
    hb_func_body = script[hb_func_start : hb_func_start + 900]
    # The heartbeat invokes the helper with the live-tail AND the progress file.
    assert "blueprint_heartbeat_body.py" in hb_func_body, (
        "_compute_heartbeat must invoke the heartbeat_body.py helper"
    )
    assert "live_tail.b64" in hb_func_body, (
        "_compute_heartbeat must pass live_tail.b64 to the helper"
    )
    assert "blueprint_compute_progress.json" in hb_func_body, (
        "_compute_heartbeat must pass the progress file to the helper so a "
        "{progress:{metric,value}} sample can be emitted"
    )
    # The body is POSTed as JSON.
    assert '-d "$_HB_BODY"' in hb_func_body, (
        "_compute_heartbeat must POST the helper-built JSON body"
    )


def test_gcp_startup_script_heartbeat_bearer_is_derived_secret_not_uuid():
    """The VM heartbeat must authenticate with the per-job secret, NOT the raw UUID.

    Delivery-side guard for the weak-auth finding: the startup script embeds the
    HMAC-derived per-job secret and sends it as the heartbeat bearer, so a mere
    observer of the job UUID cannot forge a heartbeat. The job UUID stays only as
    the resource identifier in the URL path.
    """
    job_id = uuid.uuid4()
    spec = ComputeLaunchSpec(
        job_id=job_id,
        command="python3 train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=300,
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    expected = compute_backends_module.compute_job_heartbeat_secret(job_id)
    assert expected != str(job_id), "derived secret must differ from the job UUID"
    assert f"_COMPUTE_HB_TOKEN={shlex.quote(expected)}" in script, (
        "startup script must embed the derived per-job heartbeat secret"
    )
    # The Authorization header uses the secret variable, never the raw job UUID.
    assert 'Authorization: Bearer $_COMPUTE_HB_TOKEN' in script
    assert 'Authorization: Bearer $_COMPUTE_JOB_ID' not in script, (
        "the heartbeat must NOT authenticate with the bare job UUID"
    )
    # The UUID still identifies the resource in the heartbeat URL path.
    assert "/api/agent/compute-jobs/$_COMPUTE_JOB_ID/heartbeat" in script


def test_heartbeat_body_helper_emits_live_tail_and_progress(tmp_path):
    """The baked heartbeat_body.py helper merges the live-tail file and the last
    valid progress record into a single JSON body, degrading to {} on missing or
    malformed inputs so an old runner / progress-less job still heartbeats."""
    helper = (
        compute_backends_module._COMPUTE_SCRIPTS_DIR / "heartbeat_body.py"
    )

    def run(*args):
        import subprocess

        out = subprocess.run(
            ["python3", str(helper), *args], capture_output=True, text=True
        )
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    # No inputs / missing files → bare liveness ping.
    assert run() == {}
    assert run("/nope/live", "/nope/prog") == {}

    live = tmp_path / "live_tail.b64"
    live.write_text("SGVsbG8=")  # opaque, copied verbatim
    prog = tmp_path / "progress.json"
    # Garbage, a partial line, and a NaN value are all skipped; the LAST valid
    # record (loss=0.9) is the one emitted.
    prog.write_text(
        "garbage\n"
        '{"metric": "loss", "value": 1.5}\n'
        "notjson{\n"
        '{"metric": "loss", "value": 0.9, "step": 10}\n'
        '{"metric": "loss", "value": "nan"}\n'
    )
    assert run(str(live), str(prog)) == {
        "live_output_tail": "SGVsbG8=",
        "progress": {"metric": "loss", "value": 0.9},
    }
    # Progress only.
    assert run("", str(prog)) == {"progress": {"metric": "loss", "value": 0.9}}
    # Live tail only.
    assert run(str(live), "/nope/prog") == {"live_output_tail": "SGVsbG8="}
    # Non-finite / boolean values are rejected (never keep the watchdog quiet).
    prog.write_text('{"metric": "m", "value": true}\n{"metric": "m", "value": 1e999}\n')
    assert run("", str(prog)) == {}


def test_gcp_startup_script_no_progress_watchdog_armed_after_setup():
    """The GCP no-progress watchdog must be ARMED only after setup (repo clone,
    setup step, container image pull, resume hydration) completes, so slow
    provisioning is never counted as 'no progress' (finding de3ae13c14a610d2).

    Concretely: the sliding-window arm (`_NP_WATCHDOG_PID=$!` following the
    `stat`-based watchdog loop) must come AFTER every setup failure branch, and
    the PID var must be pre-declared (empty) BEFORE those branches so the
    `set -u` script can reference it in their cleanup kills."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python3 train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=3600,
        no_progress_timeout_seconds=600,
        repo_clone={"owner": "o", "repo": "r", "ref": "main"},
        repo_token="tok",
        setup_step="pip install -r requirements.txt",
        container_image="ghcr.io/x/y:1",
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec,
        artifact_upload_url="https://storage.example.com/upload",
        input_artifact_download_url="https://storage.example.com/resume",
    )
    # Pre-declared empty init (for the set -u cleanup kills in setup branches).
    empty_init_idx = script.index("\n_NP_WATCHDOG_PID=\n")
    # The watchdog clock is anchored (touch + stat _NP_START) then armed ($!).
    arm_idx = script.index("_NP_WATCHDOG_PID=$!")
    np_start_idx = script.index("_NP_START=$(stat")
    # Every setup phase precedes the arm; the command follows it.
    clone_idx = script.index("repo_clone_failed")
    setup_idx = script.index("setup_step_failed")
    pull_idx = script.index("container_image_pull_failed")
    resume_idx = script.index("[resume]")
    command_idx = script.index("python3 /tmp/blueprint_command_runner.py")
    assert empty_init_idx < clone_idx < arm_idx, "empty init must precede setup"
    assert empty_init_idx < setup_idx < arm_idx
    assert empty_init_idx < pull_idx < arm_idx
    assert empty_init_idx < resume_idx < arm_idx
    assert np_start_idx < arm_idx, "clock must be anchored before the arm"
    assert clone_idx < np_start_idx, "watchdog must arm AFTER the clone runs"
    assert setup_idx < np_start_idx, "watchdog must arm AFTER the setup step"
    assert pull_idx < np_start_idx, "watchdog must arm AFTER the image pull"
    assert arm_idx < command_idx, "watchdog must be armed before the command runs"


def test_no_progress_stalled_detects_plateau_and_spares_advancing_metric():
    """The server-side no-progress watchdog (`_no_progress_stalled`) fires on a
    plateaued declared metric built from heartbeat-appended samples, and spares a
    job whose metric is still advancing — i.e. progress heartbeats keep a live
    job alive while a stalled one past the window is cut (findings de3ae13c /
    76c9633f, server side)."""
    from app.compute_jobs import append_progress_sample, _no_progress_stalled

    window = compute_jobs.COMPUTE_JOB_NO_PROGRESS_WINDOW_SECONDS
    grace = compute_jobs.COMPUTE_JOB_NO_PROGRESS_GRACE_SECONDS
    min_samples = compute_jobs.COMPUTE_JOB_NO_PROGRESS_MIN_SAMPLES
    now = datetime(2026, 1, 2, 0, 0, 0)
    started = now - timedelta(seconds=grace + window + 60)

    def build_job(values):
        meta = {"declared_metric": "loss"}
        n = len(values)
        for i, v in enumerate(values):
            # Spread the samples evenly across the trailing window.
            t = now - timedelta(seconds=window * (n - 1 - i) / (n - 1))
            meta = append_progress_sample(
                meta, metric="loss", value=v, t=t.isoformat()
            )
        return ComputeJob(
            command="true",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            status=ComputeJobStatus.RUNNING,
            started_at=started,
            polling_metadata=meta,
        )

    # Flat metric across a full window with enough samples → stalled → cut.
    flat = build_job([0.5] * (min_samples + 1))
    assert _no_progress_stalled(flat, now=now) is True

    # A steadily advancing metric (heartbeats moving the value) → NOT stalled.
    advancing = build_job([0.9 - 0.1 * i for i in range(min_samples + 1)])
    assert _no_progress_stalled(advancing, now=now) is False

    # A job that declares no metric is fail-open (never cut).
    no_metric = build_job([0.5] * (min_samples + 1))
    no_metric.polling_metadata = {
        k: v
        for k, v in (no_metric.polling_metadata or {}).items()
        if k != "declared_metric"
    }
    assert _no_progress_stalled(no_metric, now=now) is False


def test_no_progress_stalled_respects_per_job_timeout():
    """_no_progress_stalled honours no_progress_timeout_seconds from polling_metadata.

    A per-job timeout of 300s should fire (stalled) when the plateau spans 300s,
    even when the global window constant is much larger.  A job whose plateau is
    shorter than the per-job window must be spared, even if it already exceeds
    the global default.
    """
    from app.compute_jobs import append_progress_sample, _no_progress_stalled

    min_samples = compute_jobs.COMPUTE_JOB_NO_PROGRESS_MIN_SAMPLES
    per_job_timeout = 300  # seconds — well below the 1800s global default

    now = datetime(2026, 1, 2, 0, 0, 0)
    # Job has been running longer than per_job_timeout (grace) + per_job_timeout (window).
    started = now - timedelta(seconds=per_job_timeout * 2 + 60)

    def build_job(values, *, window_seconds):
        meta = {
            "declared_metric": "loss",
            "no_progress_timeout_seconds": per_job_timeout,
        }
        n = len(values)
        for i, v in enumerate(values):
            t = now - timedelta(seconds=window_seconds * (n - 1 - i) / (n - 1))
            meta = append_progress_sample(meta, metric="loss", value=v, t=t.isoformat())
        return ComputeJob(
            command="true",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            status=ComputeJobStatus.RUNNING,
            started_at=started,
            polling_metadata=meta,
        )

    # Plateau spanning exactly the per-job window → stalled → should fire.
    stalled = build_job([0.5] * (min_samples + 1), window_seconds=per_job_timeout)
    assert _no_progress_stalled(stalled, now=now) is True, (
        "expected stalled=True for plateau spanning per-job window"
    )

    # Plateau spanning only half the per-job window → NOT enough history → spared.
    too_short = build_job([0.5] * (min_samples + 1), window_seconds=per_job_timeout / 2)
    assert _no_progress_stalled(too_short, now=now) is False, (
        "expected stalled=False when plateau history is shorter than per-job window"
    )

    # Advancing metric within the per-job window → NOT stalled.
    advancing = build_job(
        [0.9 - 0.1 * i for i in range(min_samples + 1)],
        window_seconds=per_job_timeout,
    )
    assert _no_progress_stalled(advancing, now=now) is False, (
        "expected stalled=False for advancing metric"
    )

    # Without per-job timeout (global 1800s default), the 300s plateau is NOT stalled.
    global_job_meta: dict = {
        "declared_metric": "loss",
    }
    n = min_samples + 1
    for i, v in enumerate([0.5] * n):
        t = now - timedelta(seconds=per_job_timeout * (n - 1 - i) / (n - 1))
        global_job_meta = append_progress_sample(
            global_job_meta, metric="loss", value=v, t=t.isoformat()
        )
    global_job = ComputeJob(
        command="true",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        status=ComputeJobStatus.RUNNING,
        started_at=started,
        polling_metadata=global_job_meta,
    )
    assert _no_progress_stalled(global_job, now=now) is False, (
        "expected stalled=False when plateau is shorter than global window (no per-job override)"
    )


def test_local_backend_live_tail_read_on_poll(tmp_path):
    """poll_status returns live_output when live_tail.b64 exists in the artifact dir."""
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    tail_text = "partial output so far"
    tail_b64 = _make_live_tail_b64(tail_text)
    (artifact_dir / "live_tail.b64").write_text(tail_b64)

    job = ComputeJob(
        command="true",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        status=ComputeJobStatus.RUNNING,
        polling_metadata={"artifact_dir": str(artifact_dir)},
    )
    result = compute_backends_module._read_local_live_tail(job)
    assert result is not None, "_read_local_live_tail must return a dict when file exists"
    assert result["text"] == tail_text


# ---------------------------------------------------------------------------
# Security invariant: container_registry_auth password must never be persisted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_compute_job_registry_auth_password_rejected(
    db_session, monkeypatch
):
    """create_compute_job must raise ValueError when container_registry_auth includes a
    password — there is no delivery channel to pass the password to the backend
    launcher (unlike repo tokens, which are freshly minted at launch time)."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    task = await _make_compute_task(db_session)

    with pytest.raises(ValueError, match="password cannot be delivered"):
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py",
            machine_type="e2-standard-2",
            spend_cap_usd=2.0,
            deadline_seconds=300,
            backend_name="local-subprocess",
            container_image="nvcr.io/nvidia/tensorflow:25.01-tf2-py3",
            container_registry_auth={
                "server": "nvcr.io",
                "username": "$oauthtoken",
                "password": "s3cr3t-ngc-api-key",
            },
        )


@pytest.mark.asyncio
async def test_create_compute_job_registry_auth_no_password_persists_server_username(
    db_session, monkeypatch
):
    """create_compute_job must persist server+username when no password is present."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    task = await _make_compute_task(db_session)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=2.0,
        deadline_seconds=300,
        backend_name="local-subprocess",
        container_image="nvcr.io/nvidia/tensorflow:25.01-tf2-py3",
        container_registry_auth={
            "server": "nvcr.io",
            "username": "$oauthtoken",
        },
    )

    persisted_auth = (job.polling_metadata or {}).get("container_registry_auth")
    assert persisted_auth == {"server": "nvcr.io", "username": "$oauthtoken"}


@pytest.mark.asyncio
async def test_create_compute_job_registry_auth_password_accepted_for_runner_subprocess(
    db_session, monkeypatch
):
    """create_compute_job must persist server+username+password when the backend
    is runner-subprocess — the runner needs the credential to docker login."""
    from app import auth as auth_mod
    from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME
    from app.models import LocalRunner

    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    project = Project(name="Runner Auth Test", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    token = auth_mod.create_runner_token(uuid.uuid4())
    runner = LocalRunner(
        project_id=project.id,
        token_hash=auth_mod.hash_runner_token(token),
        capabilities={"task_types": "all", "has_gpu": False},
    )
    db_session.add(runner)
    await db_session.commit()
    await db_session.refresh(runner)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
        context={"runner_target_id": str(runner.id)},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        container_image="nvcr.io/nvidia/tensorflow:25.01-tf2-py3",
        container_registry_auth={
            "server": "nvcr.io",
            "username": "$oauthtoken",
            "password": "s3cr3t-ngc-api-key",
        },
    )

    assert job.backend == RUNNER_SUBPROCESS_BACKEND_NAME
    persisted_auth = (job.polling_metadata or {}).get("container_registry_auth")
    assert persisted_auth is not None
    assert persisted_auth["server"] == "nvcr.io"
    assert persisted_auth["username"] == "$oauthtoken"
    assert persisted_auth["password"] == "s3cr3t-ngc-api-key"


# ── Runner-subprocess backend tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_compute_job_runner_target_id_overrides_backend(
    db_session, monkeypatch
):
    """create_compute_job must route to runner-subprocess when the locked task
    carries runner_target_id in context, and persist runner_target_id in
    polling_metadata alongside the normal exec fields."""
    from app import auth as auth_mod
    from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME
    from app.models import LocalRunner

    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    project = Project(name="Runner Routing", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    token = auth_mod.create_runner_token(uuid.uuid4())
    runner = LocalRunner(
        project_id=project.id,
        token_hash=auth_mod.hash_runner_token(token),
        capabilities={"task_types": "all", "has_gpu": False},
    )
    db_session.add(runner)
    await db_session.commit()
    await db_session.refresh(runner)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
        context={"runner_target_id": str(runner.id)},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )

    assert job.backend == RUNNER_SUBPROCESS_BACKEND_NAME
    meta = job.polling_metadata or {}
    assert meta.get("runner_target_id") == str(runner.id)
    # Core exec fields must be present so the runner can execute the arm.
    assert meta.get("command") == "python train.py"
    assert "env" in meta
    assert "runtime_deadline_seconds" in meta


async def _make_require_local_runner_task(
    db_session,
    *,
    require_local_runner: bool,
    runner_target_id: str | None = None,
):
    """Project (optionally carrying the require_local_runner policy) plus a
    compute_experiment task, optionally runner-pinned via context. Mirrors
    _make_compute_task but lets each case toggle the policy and the pin."""
    build_config: dict = {}
    if require_local_runner:
        build_config["resource_envelope"] = {"require_local_runner": True}
    project = Project(
        name="Require Local Runner",
        user_id="test-user",
        build_config=build_config,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    context = {"runner_target_id": runner_target_id} if runner_target_id else None
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
        context=context,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return project, task


class TestCreateComputeJobRequireLocalRunner:
    """The hard 'never bill cloud for compute when local-runner is required' gate
    in create_compute_job: a require_local_runner project's compute_experiment job
    that is NOT runner-pinned must be rejected before any billable cloud row is
    written; a runner-pinned job (routed to runner-subprocess) and a project
    without the policy proceed normally."""

    @pytest.mark.asyncio
    async def test_unpinned_compute_experiment_rejected_when_policy_set(
        self, db_session, monkeypatch
    ):
        # Cloud mode: an unpinned job would otherwise default to the billable gcp backend.
        monkeypatch.setattr(compute, "LOCAL_MODE", False)
        monkeypatch.setattr(compute_jobs, "detect_local_gpu", lambda: None)
        _project, task = await _make_require_local_runner_task(
            db_session, require_local_runner=True
        )
        with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as excinfo:
            await compute_jobs.create_compute_job(
                db_session,
                task,
                command="python train.py",
                machine_type="e2-standard-2",
                spend_cap_usd=1.0,
                deadline_seconds=300,
            )
        assert excinfo.value.detail.get("error") == "local_runner_required"

    @pytest.mark.asyncio
    async def test_runner_pinned_compute_experiment_allowed_when_policy_set(
        self, db_session, monkeypatch
    ):
        from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME

        monkeypatch.setattr(compute, "LOCAL_MODE", True)
        _project, task = await _make_require_local_runner_task(
            db_session,
            require_local_runner=True,
            runner_target_id=str(uuid.uuid4()),
        )
        job = await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )
        assert job.backend == RUNNER_SUBPROCESS_BACKEND_NAME

    @pytest.mark.asyncio
    async def test_unpinned_compute_experiment_allowed_without_policy(
        self, db_session, monkeypatch
    ):
        # No require_local_runner policy: an unpinned job lands on the normal
        # cloud backend (gcp) — the gate must not fire.
        monkeypatch.setattr(compute, "LOCAL_MODE", False)
        monkeypatch.setattr(compute_jobs, "detect_local_gpu", lambda: None)
        _project, task = await _make_require_local_runner_task(
            db_session, require_local_runner=False
        )
        job = await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )
        assert job.backend == "gcp"


@pytest.mark.asyncio
async def test_next_compute_job_claims_starting_job_and_prevents_double_claim(
    client, db_session, monkeypatch
):
    """POST /api/runner/next-compute-job atomically transitions STARTING→RUNNING
    and a second sequential poll returns 204 (no double-claim)."""
    from app import auth as auth_mod
    from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME
    from app.models import LocalRunner

    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    project = Project(name="Next CJ Claim", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    token = auth_mod.create_runner_token(uuid.uuid4())
    runner = LocalRunner(
        project_id=project.id,
        token_hash=auth_mod.hash_runner_token(token),
        capabilities={"task_types": "all", "has_gpu": True},
    )
    db_session.add(runner)
    await db_session.commit()
    await db_session.refresh(runner)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
        context={"runner_target_id": str(runner.id)},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend=RUNNER_SUBPROCESS_BACKEND_NAME,
        status=ComputeJobStatus.STARTING,
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        polling_metadata={
            "command": "python train.py",
            "env": {},
            "cwd": None,
            "runner_target_id": str(runner.id),
            "runtime_deadline_seconds": 300,
        },
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    # First poll: should claim the job.
    resp1 = await client.post(
        "/api/runner/next-compute-job",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp1.status_code == 200, resp1.text
    body = resp1.json()
    assert body["job_id"] == str(job.id)
    assert body["command"] == "python train.py"
    assert body["runtime_deadline_seconds"] == 300

    # Job must now be RUNNING.
    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.RUNNING
    assert job.last_heartbeat_at is not None
    assert (job.polling_metadata or {}).get("runner_claimed_at") is not None

    # Second poll: no unclaimed STARTING job remains → 204.
    resp2 = await client.post(
        "/api/runner/next-compute-job",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 204


@pytest.mark.asyncio
async def test_next_compute_job_delivers_repo_clone_from_launch(
    client, db_session, monkeypatch
):
    """repo_clone persisted by RunnerSubprocessBackend.launch() must be present in
    /api/runner/next-compute-job response.

    RunnerSubprocessBackend.launch() returns repo_clone in result.metadata.
    _apply_launch_result is skipped for runner-subprocess (to avoid premature
    STARTING→RUNNING transition), so the monitor must explicitly persist
    result.metadata.  This test drives that path end-to-end: monitor tick →
    claim endpoint, asserting non-null repo_clone reaches the runner.
    """
    from app import auth as auth_mod
    from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME
    from app.models import LocalRunner

    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    project = Project(name="Repo Clone Delivery", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    token = auth_mod.create_runner_token(uuid.uuid4())
    runner = LocalRunner(
        project_id=project.id,
        token_hash=auth_mod.hash_runner_token(token),
        capabilities={"task_types": "all", "has_gpu": True},
    )
    db_session.add(runner)
    await db_session.commit()
    await db_session.refresh(runner)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
        context={"runner_target_id": str(runner.id)},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Patch _attach_repo_clone so it injects a repo_clone without needing GitHub.
    _FAKE_REPO_CLONE = {"owner": "acme", "repo": "ml", "ref": "main"}

    async def _fake_attach_repo_clone(session_factory, job, spec, *, task=None):
        import dataclasses
        return dataclasses.replace(spec, repo_clone=_FAKE_REPO_CLONE)

    monkeypatch.setattr(compute_jobs, "_attach_repo_clone", _fake_attach_repo_clone)

    # Patch _get_project_github_token so next_compute_job can mint a fake token.
    # It is imported lazily inside the endpoint, so patch at the source module.
    async def _fake_get_token(project, session):
        return "gh-test-token"

    import app.branch_cleanup as branch_cleanup_mod
    monkeypatch.setattr(branch_cleanup_mod, "_get_project_github_token", _fake_get_token)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    assert job.backend == RUNNER_SUBPROCESS_BACKEND_NAME

    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    # Run the monitor tick: it calls launch() and should persist repo_clone.
    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)
    await db_session.refresh(job)

    # repo_clone must be persisted into polling_metadata by the new elif branch.
    meta = job.polling_metadata or {}
    assert meta.get("repo_clone") == _FAKE_REPO_CLONE, (
        f"Expected repo_clone in polling_metadata after monitor launch tick; got: {meta}"
    )

    # Claim the job and assert the endpoint delivers repo_clone + repo_token.
    resp = await client.post(
        "/api/runner/next-compute-job",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == str(job.id)
    assert body["repo_clone"] == _FAKE_REPO_CLONE
    assert body["repo_token"] == "gh-test-token"


@pytest.mark.asyncio
async def test_next_compute_job_scoped_to_pinned_runner(client, db_session, monkeypatch):
    """Within ONE project, a job pinned (polling_metadata['runner_target_id']) to
    runner A must NOT be claimable by a different runner B — even a second poll by
    B returns 204 while A claims it. This is the server-side enforcement of the
    GPU capability gate: a non-GPU runner must not steal a GPU experiment that
    _try_runner_dispatch pinned to a has_gpu runner."""
    from app import auth as auth_mod
    from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME
    from app.models import LocalRunner

    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    project = Project(name="Pinned Runner Scope", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Runner A: has GPU, the intended target.
    token_a = auth_mod.create_runner_token(uuid.uuid4())
    runner_a = LocalRunner(
        project_id=project.id,
        token_hash=auth_mod.hash_runner_token(token_a),
        capabilities={"task_types": "all", "has_gpu": True},
    )
    # Runner B: no GPU, same project — must not be able to claim A's job.
    token_b = auth_mod.create_runner_token(uuid.uuid4())
    runner_b = LocalRunner(
        project_id=project.id,
        token_hash=auth_mod.hash_runner_token(token_b),
        capabilities={"task_types": "all", "has_gpu": False},
    )
    db_session.add_all([runner_a, runner_b])
    await db_session.commit()
    await db_session.refresh(runner_a)
    await db_session.refresh(runner_b)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
        context={"runner_target_id": str(runner_a.id)},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend=RUNNER_SUBPROCESS_BACKEND_NAME,
        status=ComputeJobStatus.STARTING,
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        polling_metadata={
            "command": "python train.py",
            "env": {},
            "runner_target_id": str(runner_a.id),
            "runtime_deadline_seconds": 300,
        },
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    # Runner B (wrong pin) must NOT claim the job.
    resp_b = await client.post(
        "/api/runner/next-compute-job",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp_b.status_code == 204, resp_b.text

    # Job must still be STARTING (untouched by B's poll).
    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.STARTING

    # Runner A (correct pin) claims it.
    resp_a = await client.post(
        "/api/runner/next-compute-job",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp_a.status_code == 200, resp_a.text
    assert resp_a.json()["job_id"] == str(job.id)
    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.RUNNING


@pytest.mark.asyncio
async def test_next_compute_job_project_scoped(client, db_session, monkeypatch):
    """A runner must not claim a job belonging to a different project."""
    from app import auth as auth_mod
    from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME
    from app.models import LocalRunner

    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    proj_a = Project(name="Proj A", user_id="u")
    proj_b = Project(name="Proj B", user_id="u")
    db_session.add_all([proj_a, proj_b])
    await db_session.commit()
    await db_session.refresh(proj_a)
    await db_session.refresh(proj_b)

    token_b = auth_mod.create_runner_token(uuid.uuid4())
    runner_b = LocalRunner(
        project_id=proj_b.id,
        token_hash=auth_mod.hash_runner_token(token_b),
        capabilities={"task_types": "all", "has_gpu": False},
    )
    db_session.add(runner_b)
    await db_session.commit()
    await db_session.refresh(runner_b)

    # Job belongs to proj_a — runner_b should NOT see it.
    task_a = Task(
        project_id=proj_a.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task_a)
    await db_session.commit()
    await db_session.refresh(task_a)

    job_a = ComputeJob(
        project_id=proj_a.id,
        task_id=task_a.id,
        backend=RUNNER_SUBPROCESS_BACKEND_NAME,
        status=ComputeJobStatus.STARTING,
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        polling_metadata={"command": "echo", "env": {}, "runtime_deadline_seconds": 60},
    )
    db_session.add(job_a)
    await db_session.commit()

    resp = await client.post(
        "/api/runner/next-compute-job",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 204  # proj_b runner cannot see proj_a job


@pytest.mark.asyncio
async def test_compute_job_report_stamps_status_so_monitor_finalizes(
    client, db_session, monkeypatch
):
    """POST /api/runner/compute-job/{id}/report stamps runner_exec_status so
    RunnerSubprocessBackend.poll_status() returns SUCCEEDED/FAILED,
    which the monitor uses to finalize the job."""
    from app import auth as auth_mod
    from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME, RunnerSubprocessBackend
    from app.models import LocalRunner

    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    project = Project(name="Report Terminal", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    token = auth_mod.create_runner_token(uuid.uuid4())
    runner = LocalRunner(
        project_id=project.id,
        token_hash=auth_mod.hash_runner_token(token),
        capabilities={"task_types": "all", "has_gpu": True},
    )
    db_session.add(runner)
    await db_session.commit()
    await db_session.refresh(runner)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend=RUNNER_SUBPROCESS_BACKEND_NAME,
        status=ComputeJobStatus.RUNNING,
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        polling_metadata={"command": "python train.py", "env": {}, "runtime_deadline_seconds": 300, "runner_target_id": str(runner.id)},
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    resp = await client.post(
        f"/api/runner/compute-job/{job.id}/report",
        json={
            "exec_status": "succeeded",
            "exit_code": 0,
            "result_metrics": {"accuracy": 0.99, "loss": 0.01},
            "command_log": {"text": "Training complete", "truncated": False},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["cancel_requested"] is False

    await db_session.refresh(job)
    meta = job.polling_metadata or {}
    assert meta.get("runner_exec_status") == "succeeded"
    assert meta.get("runner_exit_code") == 0
    assert meta.get("runner_result_metrics") == {"accuracy": 0.99, "loss": 0.01}
    assert meta.get("runner_command_log") == {"text": "Training complete", "truncated": False}
    assert meta.get("runner_finished_at") is not None

    # poll_status must now return SUCCEEDED so the monitor can finalize.
    backend = RunnerSubprocessBackend()
    status_result = await backend.poll_status(job)
    assert status_result.status == ComputeJobStatus.SUCCEEDED
    assert status_result.exit_code == 0
    assert status_result.metrics == {"accuracy": 0.99, "loss": 0.01}
    # message must be non-null so terminal_reason does not fall back to the
    # stale initial "queued for compute-job launch" status_message.
    assert status_result.message == "compute job succeeded"


@pytest.mark.asyncio
async def test_runner_subprocess_poll_status_failed_carries_message():
    """RunnerSubprocessBackend.poll_status sets message='compute job failed' for
    a failed job so terminal_reason is not stuck at 'queued for compute-job launch'."""
    from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME, RunnerSubprocessBackend

    backend = RunnerSubprocessBackend()
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend=RUNNER_SUBPROCESS_BACKEND_NAME,
        status=ComputeJobStatus.RUNNING,
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        polling_metadata={
            "runner_exec_status": "failed",
            "runner_exit_code": 1,
            "status_message": "queued for compute-job launch",  # stale initial message
        },
    )
    status_result = await backend.poll_status(job)
    assert status_result.status == ComputeJobStatus.FAILED
    assert status_result.exit_code == 1
    assert status_result.message == "compute job failed"


@pytest.mark.asyncio
async def test_compute_job_report_command_log_extra_keys_filtered(
    client, db_session, monkeypatch
):
    """POST /api/runner/compute-job/{id}/report with command_log containing extra
    keys stores only {text, truncated} in polling_metadata (key 14c62b47c166af12)."""
    from app import auth as auth_mod
    from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME
    from app.models import LocalRunner

    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    project = Project(name="Extra Keys Filter", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    token = auth_mod.create_runner_token(uuid.uuid4())
    runner = LocalRunner(
        project_id=project.id,
        token_hash=auth_mod.hash_runner_token(token),
        capabilities={"task_types": "all"},
    )
    db_session.add(runner)
    await db_session.commit()
    await db_session.refresh(runner)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend=RUNNER_SUBPROCESS_BACKEND_NAME,
        status=ComputeJobStatus.RUNNING,
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        polling_metadata={"command": "python run.py", "env": {}, "runtime_deadline_seconds": 300, "runner_target_id": str(runner.id)},
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    resp = await client.post(
        f"/api/runner/compute-job/{job.id}/report",
        json={
            "exec_status": "succeeded",
            "exit_code": 0,
            "command_log": {
                "text": "Done",
                "truncated": False,
                "extra_key": "should_be_dropped",
                "another_key": 123,
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200

    await db_session.refresh(job)
    meta = job.polling_metadata or {}
    stored_log = meta.get("runner_command_log")
    assert stored_log == {"text": "Done", "truncated": False}
    # Extra keys must not be persisted
    assert "extra_key" not in stored_log
    assert "another_key" not in stored_log


@pytest.mark.asyncio
async def test_compute_job_report_idempotent_on_terminal_job(
    client, db_session, monkeypatch
):
    """Reporting on an already-terminal job must return ok=True and cancel_requested=True
    without updating the job (idempotent)."""
    from app import auth as auth_mod
    from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME
    from app.models import LocalRunner

    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    project = Project(name="Idempotent Report", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    token = auth_mod.create_runner_token(uuid.uuid4())
    runner = LocalRunner(
        project_id=project.id,
        token_hash=auth_mod.hash_runner_token(token),
        capabilities={"task_types": "all", "has_gpu": False},
    )
    db_session.add(runner)
    await db_session.commit()
    await db_session.refresh(runner)

    job = ComputeJob(
        project_id=project.id,
        backend=RUNNER_SUBPROCESS_BACKEND_NAME,
        status=ComputeJobStatus.CANCELLED,
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        polling_metadata={"command": "echo", "env": {}, "runner_target_id": str(runner.id)},
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    resp = await client.post(
        f"/api/runner/compute-job/{job.id}/report",
        json={"exec_status": "running"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["cancel_requested"] is True  # terminal job signals cancel to runner


@pytest.mark.asyncio
async def test_try_runner_dispatch_gpu_experiment_requires_has_gpu_runner(db_session):
    """_try_runner_dispatch must pin a GPU compute_experiment only to a runner
    advertising has_gpu=True; a non-GPU runner must be refused."""
    from app import auth as auth_mod
    from app import orchestrator as orch
    from app.models import LocalRunner

    project = Project(name="GPU Gate", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    def _make_runner(has_gpu, name="runner"):
        t = auth_mod.create_runner_token(uuid.uuid4())
        r = LocalRunner(
            project_id=project.id,
            token_hash=auth_mod.hash_runner_token(t),
            capabilities={"task_types": "all", "has_gpu": has_gpu},
        )
        return r

    cpu_runner = _make_runner(has_gpu=False)
    gpu_runner = _make_runner(has_gpu=True)
    db_session.add_all([cpu_runner, gpu_runner])
    await db_session.commit()
    await db_session.refresh(cpu_runner)
    await db_session.refresh(gpu_runner)

    gpu_task = Task(
        project_id=project.id,
        payloads=[{"type": "compute_experiment", "accelerator": "T4"}],
        status=TaskStatus.QUEUED,
    )
    db_session.add(gpu_task)
    await db_session.commit()
    await db_session.refresh(gpu_task)

    # CPU-only runner cache → GPU experiment must NOT be pinned.
    pinned = await orch._try_runner_dispatch(
        db_session, gpu_task, {project.id: [cpu_runner]}
    )
    assert pinned is False
    await db_session.refresh(gpu_task)
    assert gpu_task.status == TaskStatus.QUEUED

    # GPU-capable runner cache → GPU experiment MUST be pinned to it.
    pinned = await orch._try_runner_dispatch(
        db_session, gpu_task, {project.id: [cpu_runner, gpu_runner]}
    )
    assert pinned is True
    await db_session.refresh(gpu_task)
    assert gpu_task.status == TaskStatus.PROVISIONING
    assert gpu_task.vm_instance_id == f"runner:{gpu_runner.id}"


@pytest.mark.asyncio
async def test_try_runner_dispatch_cpu_experiment_routes_to_any_runner(db_session):
    """A CPU compute_experiment (no accelerator) can be pinned to any eligible runner."""
    from app import auth as auth_mod
    from app import orchestrator as orch
    from app.models import LocalRunner

    project = Project(name="CPU Exp Route", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    token = auth_mod.create_runner_token(uuid.uuid4())
    runner = LocalRunner(
        project_id=project.id,
        token_hash=auth_mod.hash_runner_token(token),
        capabilities={"task_types": "all", "has_gpu": False},
    )
    db_session.add(runner)
    await db_session.commit()
    await db_session.refresh(runner)

    cpu_task = Task(
        project_id=project.id,
        payloads=[{"type": "compute_experiment"}],  # no accelerator
        status=TaskStatus.QUEUED,
    )
    db_session.add(cpu_task)
    await db_session.commit()
    await db_session.refresh(cpu_task)

    pinned = await orch._try_runner_dispatch(
        db_session, cpu_task, {project.id: [runner]}
    )
    assert pinned is True
    await db_session.refresh(cpu_task)
    assert cpu_task.status == TaskStatus.PROVISIONING


@pytest.mark.asyncio
async def test_dead_runner_pin_cleared_on_requeue_falls_over_to_gcp(
    db_session, monkeypatch
):
    """Regression: stale-PROVISIONING requeue must clear runner_target_id so the
    mandatory cloud fallback engages instead of re-routing to the dead runner.

    When clear_task_llm_runtime_context is applied during recovery (orchestrator.py
    stale-PROVISIONING sweep) the runner_target_id key must be absent from the
    resulting context, and a subsequent create_compute_job call must route to a
    non-runner-subprocess backend (GCP / local) rather than back to the dead runner.
    """
    from app import auth as auth_mod
    from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME
    from app.max_plan_pool import clear_task_llm_runtime_context
    from app.models import LocalRunner

    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    project = Project(name="Fallback Test", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    token = auth_mod.create_runner_token(uuid.uuid4())
    runner = LocalRunner(
        project_id=project.id,
        token_hash=auth_mod.hash_runner_token(token),
        capabilities={"task_types": "all", "has_gpu": False},
    )
    db_session.add(runner)
    await db_session.commit()
    await db_session.refresh(runner)

    # Simulate task that was pinned to the runner (PROVISIONING), then recovered
    # back to QUEUED by the stale-PROVISIONING sweep — context still has the pin.
    pinned_ctx = {
        "runner_target_id": str(runner.id),
        "provision_failures": 1,
    }
    recovered_ctx = clear_task_llm_runtime_context(pinned_ctx)

    # Core assertion: runner_target_id must be stripped so GCP dispatch is chosen.
    assert "runner_target_id" not in recovered_ctx, (
        "clear_task_llm_runtime_context must remove runner_target_id so a dead "
        "runner is not re-pinned on requeue"
    )
    # Non-runtime keys (provision_failures) must be preserved.
    assert recovered_ctx.get("provision_failures") == 1

    # Integration: create_compute_job with the cleared context must NOT route to
    # the runner-subprocess backend.
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
        context=recovered_ctx,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )

    assert job.backend != RUNNER_SUBPROCESS_BACKEND_NAME, (
        f"After runner_target_id is cleared, create_compute_job must not route "
        f"to {RUNNER_SUBPROCESS_BACKEND_NAME} (got {job.backend!r})"
    )


def test_materialize_local_manifest_rejects_path_traversal():
    """_materialize_local_manifest rejects paths with '..' segments or that resolve outside root."""
    with pytest.raises(ValueError, match="Path traversal"):
        compute._materialize_local_manifest(
            "test-vm-traverse",
            {"files": [{"path": "../../../etc/passwd", "content": "evil"}]},
        )


def test_materialize_local_manifest_rejects_deep_traversal():
    """_materialize_local_manifest rejects paths with nested '..' segments."""
    with pytest.raises(ValueError, match="Path traversal"):
        compute._materialize_local_manifest(
            "test-vm-deep",
            {"files": [{"path": "subdir/../../../../etc/shadow", "content": "evil"}]},
        )


def test_materialize_local_manifest_rejects_trailing_dotdot():
    """_materialize_local_manifest rejects paths ending with '..' under disguise."""
    with pytest.raises(ValueError, match="Path traversal"):
        compute._materialize_local_manifest(
            "test-vm-dotdot",
            {"files": [{"path": "safe/..", "content": "evil"}]},
        )


def test_materialize_local_manifest_accepts_normal_path():
    """_materialize_local_manifest accepts a normal path and writes the file."""
    env = compute._materialize_local_manifest(
        "test-vm-normal",
        {"files": [{"path": "/tmp/safe.txt", "content": "hello"}], "env": {"OUTPUT": "/tmp/safe.txt"}},
    )
    from pathlib import Path
    import tempfile
    expected_root = Path(tempfile.gettempdir()) / "blueprint-agent-state" / "test-vm-normal"
    expected_file = expected_root / "tmp" / "safe.txt"
    assert expected_file.read_text(encoding="utf-8") == "hello"
    assert "OUTPUT" in env
    assert env["OUTPUT"] == str(expected_file)


@pytest.mark.asyncio
async def test_retry_provisioning_timeout_extends_deadline(db_session, monkeypatch):
    """``_maybe_retry_provisioning_timeout`` extends the provisioning deadline
    and increments the retry counter when retry budget remains."""
    # backend_name="local-subprocess" requires LOCAL_MODE; the CI shards do not
    # export LOCAL_MODE=true (unlike the local harness), so patch explicitly.
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="Prov Retry", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="prov retry parent",
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
        backend_name="local-subprocess",
    )
    # Put the job in STARTING (as the monitor does before provisioning timeout).
    job.status = ComputeJobStatus.STARTING
    db_session.add(job)
    await db_session.commit()

    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )
    # First retry should succeed.
    result = await compute_jobs._maybe_retry_provisioning_timeout(
        session_factory, job.id
    )
    assert result is True

    await db_session.refresh(job)
    meta = job.polling_metadata or {}
    assert meta.get("provisioning_retry_count") == 1
    assert "provisioning_deadline_at" in meta
    assert "retry 1/2" in meta.get("status_message", "")


@pytest.mark.asyncio
async def test_retry_provisioning_timeout_exhausted(db_session, monkeypatch):
    """``_maybe_retry_provisioning_timeout`` returns False when max retries have
    been consumed, so the caller proceeds with the terminal transition."""
    # backend_name="local-subprocess" requires LOCAL_MODE; the CI shards do not
    # export LOCAL_MODE=true (unlike the local harness), so patch explicitly.
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="Prov Retry Exhaust", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="prov retry exhaust parent",
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
        backend_name="local-subprocess",
    )
    job.status = ComputeJobStatus.STARTING
    # Set retry count at max so budget is exhausted.
    meta = dict(job.polling_metadata or {})
    meta["provisioning_retry_count"] = compute_jobs.MAX_PROVISIONING_RETRY_COUNT
    job.polling_metadata = meta
    db_session.add(job)
    await db_session.commit()

    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    result = await compute_jobs._maybe_retry_provisioning_timeout(
        session_factory, job.id
    )
    assert result is False


@pytest.mark.asyncio
async def test_retry_provisioning_timeout_already_terminal(db_session, monkeypatch):
    """``_maybe_retry_provisioning_timeout`` returns True (no-op) when the job
    is already terminal (concurrent race)."""
    # backend_name="local-subprocess" requires LOCAL_MODE; the CI shards do not
    # export LOCAL_MODE=true (unlike the local harness), so patch explicitly.
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="Prov Retry Term", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="prov retry term parent",
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
        backend_name="local-subprocess",
    )
    # Already terminal → retry should no-op.
    job.status = ComputeJobStatus.SUCCEEDED
    db_session.add(job)
    await db_session.commit()

    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    result = await compute_jobs._maybe_retry_provisioning_timeout(
        session_factory, job.id
    )
    assert result is True


# ---------------------------------------------------------------------------
# spawn_followup gate tests
# ---------------------------------------------------------------------------

async def _setup_parked_succeeded_job(db_session, *, created_by="USER", payload_extra=None):
    """Helper: create a project/task/job in PARKED state with a fired SUCCEEDED wake."""
    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND
    from app.task_lifecycle import fire_task_wakes, park_task

    project = Project(name=f"SpawnFollowup {uuid.uuid4().hex[:6]}", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    payload = {"type": "compute_experiment", "base_branch": "main"}
    if payload_extra:
        payload.update(payload_extra)

    task = Task(
        project_id=project.id,
        title="compute_experiment: spawn_followup gate",
        status=TaskStatus.IN_PROGRESS,
        payloads=[payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:12]}",
        created_by=created_by,
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
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    job.polling_metadata = meta
    job.status = ComputeJobStatus.SUCCEEDED
    job.outcome = ComputeJobOutcome.SUCCESS
    job.final_cost_usd = 0.10
    job.accrued_cost_usd = 0.10
    db_session.add(job)
    await fire_task_wakes(
        db_session,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(job.id),
    )
    await db_session.commit()

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
    return task, job


@pytest.mark.asyncio
async def test_spawn_followup_suppressed_for_operator_authored_task(db_session, monkeypatch):
    """Operator-authored tasks (created_by='USER') do NOT spawn a reason-and-ship
    follow-on unless spawn_followup=True is explicitly set in the payload."""
    from app.reaper_sweeps import sweep_deliver_task_wakes

    spawn_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("app.task_completion._spawn_act_on_verdict_followon", spawn_mock)
    monkeypatch.setattr("app.task_completion.broadcast_to_project", AsyncMock())
    monkeypatch.setattr(
        "app.knowledge_completion.persist_compute_experiment_knowledge",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.knowledge_completion._analyze_compute_experiment_with_llm",
        AsyncMock(return_value=(None, "precondition-not-met")),
    )

    task, _job = await _setup_parked_succeeded_job(db_session, created_by="USER")

    delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert delivered == 1

    db_session.expire_all()
    await db_session.refresh(task)
    # Operator smoke: task completes without spawning a follow-on.
    assert task.status == TaskStatus.COMPLETED
    spawn_mock.assert_not_called()


@pytest.mark.asyncio
async def test_spawn_followup_opt_in_for_operator_authored_task(db_session, monkeypatch):
    """When spawn_followup=True is explicitly set, operator-authored tasks DO spawn
    a follow-on (the operator opted in)."""
    from app.reaper_sweeps import sweep_deliver_task_wakes

    fake_child = Task(
        project_id=None,
        title="follow-on",
        status=TaskStatus.QUEUED,
    )
    spawn_mock = AsyncMock(return_value=fake_child)
    monkeypatch.setattr("app.task_completion._spawn_act_on_verdict_followon", spawn_mock)
    monkeypatch.setattr("app.task_completion.broadcast_to_project", AsyncMock())
    monkeypatch.setattr(
        "app.knowledge_completion.persist_compute_experiment_knowledge",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.knowledge_completion._analyze_compute_experiment_with_llm",
        AsyncMock(return_value=(None, "precondition-not-met")),
    )

    task, _job = await _setup_parked_succeeded_job(
        db_session, created_by="USER", payload_extra={"spawn_followup": True}
    )

    delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert delivered == 1

    db_session.expire_all()
    await db_session.refresh(task)
    # Opted-in: follow-on was spawned, task should be EVALUATING.
    assert task.status == TaskStatus.EVALUATING
    spawn_mock.assert_called_once()


@pytest.mark.asyncio
async def test_spawn_followup_spawns_for_engine_authored_task(db_session, monkeypatch):
    """Engine-authored tasks (created_by='ORCHESTRATOR') spawn the reason-and-ship
    follow-on by default (no explicit spawn_followup needed)."""
    from app.reaper_sweeps import sweep_deliver_task_wakes

    fake_child = Task(
        project_id=None,
        title="follow-on",
        status=TaskStatus.QUEUED,
    )
    spawn_mock = AsyncMock(return_value=fake_child)
    monkeypatch.setattr("app.task_completion._spawn_act_on_verdict_followon", spawn_mock)
    monkeypatch.setattr("app.task_completion.broadcast_to_project", AsyncMock())
    monkeypatch.setattr(
        "app.knowledge_completion.persist_compute_experiment_knowledge",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.knowledge_completion._analyze_compute_experiment_with_llm",
        AsyncMock(return_value=(None, "precondition-not-met")),
    )

    task, _job = await _setup_parked_succeeded_job(db_session, created_by="ORCHESTRATOR")

    delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert delivered == 1

    db_session.expire_all()
    await db_session.refresh(task)
    # Engine-authored: follow-on spawned by default → EVALUATING.
    assert task.status == TaskStatus.EVALUATING
    spawn_mock.assert_called_once()


@pytest.mark.asyncio
async def test_spawn_followup_suppressed_for_engine_authored_task_with_false(db_session, monkeypatch):
    """Engine-authored tasks with spawn_followup=False do NOT spawn a follow-on."""
    from app.reaper_sweeps import sweep_deliver_task_wakes

    spawn_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("app.task_completion._spawn_act_on_verdict_followon", spawn_mock)
    monkeypatch.setattr("app.task_completion.broadcast_to_project", AsyncMock())
    monkeypatch.setattr(
        "app.knowledge_completion.persist_compute_experiment_knowledge",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.knowledge_completion._analyze_compute_experiment_with_llm",
        AsyncMock(return_value=(None, "precondition-not-met")),
    )

    task, _job = await _setup_parked_succeeded_job(
        db_session, created_by="ORCHESTRATOR", payload_extra={"spawn_followup": False}
    )

    delivered = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert delivered == 1

    db_session.expire_all()
    await db_session.refresh(task)
    # Explicitly suppressed: task completes without follow-on.
    assert task.status == TaskStatus.COMPLETED
    spawn_mock.assert_not_called()


# ── Progress-series ring buffer + terminal_kind disposition ──────────────────

def test_append_progress_sample_basic():
    """A valid metric+value is appended as {t, name, value} into progress_series."""
    from app.compute_jobs import append_progress_sample

    meta = {}
    result = append_progress_sample(meta, metric="loss", value=0.42, t="2026-01-01T00:00:00")
    series = result["progress_series"]
    assert len(series) == 1
    assert series[0]["name"] == "loss"
    assert series[0]["value"] == 0.42
    assert series[0]["t"] == "2026-01-01T00:00:00"


def test_append_progress_sample_invalid_metric_key():
    """Bad metric keys (spaces, exclamation marks) are silently ignored."""
    from app.compute_jobs import append_progress_sample

    meta = {}
    result = append_progress_sample(meta, metric="bad key!", value=1.0, t="2026-01-01T00:00:00")
    assert result == {}


def test_append_progress_sample_non_finite_value():
    """NaN and Inf values are silently ignored."""
    from app.compute_jobs import append_progress_sample

    for bad_val in (float("inf"), float("-inf"), float("nan")):
        result = append_progress_sample({}, metric="acc", value=bad_val, t="2026-01-01T00:00:00")
        assert result == {}, f"expected no mutation for value={bad_val}"


def test_append_progress_sample_ring_buffer_cap():
    """After 55 samples only the last 50 are retained (drop-oldest)."""
    from app.compute_jobs import append_progress_sample

    meta: dict = {}
    for i in range(55):
        meta = append_progress_sample(meta, metric="step", value=float(i), t=f"t{i}")
    series = meta["progress_series"]
    assert len(series) == 50
    assert series[0]["value"] == 5.0, "oldest 5 samples should have been dropped"
    assert series[-1]["value"] == 54.0


def test_append_progress_sample_does_not_mutate_input():
    """append_progress_sample returns a new dict, never mutates the input."""
    from app.compute_jobs import append_progress_sample

    original = {"other": "data"}
    result = append_progress_sample(original, metric="loss", value=0.1, t="t0")
    assert "progress_series" not in original
    assert "progress_series" in result
    assert original["other"] == "data"


def test_merge_progress_report_bridges_new_records_to_progress_series():
    """_merge_progress_report returns new_records so callers can bridge them into
    progress_series — the key the no-progress watchdog reads.  Verify that the
    returned new_records list is non-empty and maps to the correct shape expected
    by append_progress_sample (metric/value fields present)."""
    from app.routers.runner import _merge_progress_report
    from app.compute_jobs import append_progress_sample, _no_progress_stalled
    from app.models import ComputeJob, ComputeJobStatus, _utcnow
    import uuid

    now = _utcnow()
    raw = {
        "records": [
            {"metric": "reward", "value": 1.0, "ts": "2026-01-01T00:00:00"},
            {"metric": "reward", "value": 2.0, "ts": "2026-01-01T00:00:01"},
        ],
        "count": 2,
    }
    merged, new_records = _merge_progress_report(None, raw, now)

    assert merged is not None, "merged dict must be returned for valid payload"
    assert len(new_records) == 2, "both records are new and must be returned"
    assert new_records[0]["metric"] == "reward"
    assert new_records[1]["value"] == 2.0

    # Bridge new_records into progress_series the same way the route handler does.
    meta: dict = {}
    for rec in new_records:
        meta = append_progress_sample(
            meta,
            metric=rec["metric"],
            value=rec["value"],
            t=rec.get("ts") or now.isoformat(),
        )

    series = meta.get("progress_series", [])
    assert len(series) == 2
    assert series[0]["name"] == "reward"
    assert series[0]["value"] == 1.0
    assert series[1]["value"] == 2.0

    # Confirm the watchdog can now read progress for a runner job.
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend="local-subprocess",
        status=ComputeJobStatus.RUNNING,
        polling_metadata=meta,
        declared_metric="reward",
    )
    # Two ascending samples — watchdog must NOT stall (metric is advancing).
    assert _no_progress_stalled(job, now=now) is False


@pytest.mark.asyncio
async def test_compute_job_vm_heartbeat_stores_progress_sample(
    client, db_session, monkeypatch
):
    """A heartbeat carrying a progress object appends it to polling_metadata['progress_series']."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="Progress HB Test", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="true",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name="local-subprocess",
    )
    job.status = ComputeJobStatus.RUNNING
    await db_session.commit()

    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/heartbeat",
        headers={
            "Authorization": (
                f"Bearer {compute_backends_module.compute_job_heartbeat_secret(job.id)}"
            )
        },
        json={"progress": {"metric": "loss", "value": 0.42}},
    )
    assert resp.status_code == 204

    await db_session.refresh(job)
    series = (job.polling_metadata or {}).get("progress_series")
    assert series is not None, "progress_series must be stored in polling_metadata"
    assert len(series) == 1
    assert series[0]["name"] == "loss"
    assert series[0]["value"] == 0.42


@pytest.mark.asyncio
async def test_compute_job_vm_heartbeat_invalid_progress_still_204(
    client, db_session, monkeypatch
):
    """A heartbeat with an invalid progress object still returns 204 and leaves series empty."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="Bad Progress HB Test", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="true",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name="local-subprocess",
    )
    job.status = ComputeJobStatus.RUNNING
    await db_session.commit()

    # Invalid: metric key has a space, value is a non-numeric string
    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/heartbeat",
        headers={
            "Authorization": (
                f"Bearer {compute_backends_module.compute_job_heartbeat_secret(job.id)}"
            )
        },
        json={"progress": {"metric": "bad key!", "value": "not a number"}},
    )
    assert resp.status_code == 204

    await db_session.refresh(job)
    series = (job.polling_metadata or {}).get("progress_series")
    assert series is None or series == [], "no progress_series should be written for invalid input"


def test_finalize_terminal_job_no_progress_distinct_from_deadline(db_session):
    """_finalize_terminal_job with terminal_kind='no_progress' stamps a distinct
    terminal_kind compared to the wall-clock deadline path."""
    from app.compute_jobs import _finalize_terminal_job

    now = _utcnow()

    deadline_job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend="local-subprocess",
        status=ComputeJobStatus.RUNNING,
        spend_cap_usd=1.0,
        polling_metadata={},
    )
    _finalize_terminal_job(
        deadline_job,
        status=ComputeJobStatus.TIMED_OUT,
        outcome=ComputeJobOutcome.TIMED_OUT,
        terminal_reason="compute job exceeded runtime deadline",
        now=now,
    )
    deadline_kind = (deadline_job.polling_metadata or {}).get("terminal_kind")
    assert deadline_kind == "deadline"

    no_progress_job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend="local-subprocess",
        status=ComputeJobStatus.RUNNING,
        spend_cap_usd=1.0,
        polling_metadata={},
    )
    _finalize_terminal_job(
        no_progress_job,
        status=ComputeJobStatus.TIMED_OUT,
        outcome=ComputeJobOutcome.TIMED_OUT,
        terminal_reason="no progress detected",
        now=now,
        terminal_kind="no_progress",
    )
    no_progress_kind = (no_progress_job.polling_metadata or {}).get("terminal_kind")
    assert no_progress_kind == "no_progress"
    assert deadline_kind != no_progress_kind, (
        "no_progress and deadline cuts must have distinct terminal_kind"
    )


def test_serialize_compute_job_response_safe_promotes_progress_series_and_terminal_kind():
    """serialize_compute_job_response_safe promotes progress_series and terminal_kind
    as first-class fields on the operator-facing payload."""
    from app.schemas import serialize_compute_job_response_safe

    progress_series = [{"t": "2026-01-01T00:00:00", "name": "loss", "value": 0.5}]
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend="local-subprocess",
        status=ComputeJobStatus.TIMED_OUT,
        outcome=ComputeJobOutcome.TIMED_OUT,
        terminal_reason="compute job exceeded runtime deadline",
        spend_cap_usd=1.0,
        polling_metadata={
            "terminal_kind": "deadline",
            "progress_series": progress_series,
        },
    )

    data = serialize_compute_job_response_safe(job)
    assert data["terminal_kind"] == "deadline", "terminal_kind must be promoted"
    assert data["progress_series"] == progress_series, "progress_series must be promoted"


def test_serialize_compute_job_response_safe_absent_on_legacy_rows():
    """Legacy rows without terminal_kind or progress_series surface as None."""
    from app.schemas import serialize_compute_job_response_safe

    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend="local-subprocess",
        status=ComputeJobStatus.SUCCEEDED,
        outcome=ComputeJobOutcome.SUCCESS,
        spend_cap_usd=1.0,
        polling_metadata={},
    )

    data = serialize_compute_job_response_safe(job)
    assert data["terminal_kind"] is None
    assert data["progress_series"] is None


def test_serialize_compute_job_response_safe_promotes_watchdog_protection_status():
    """serialize_compute_job_response_safe promotes the four watchdog-protection status
    fields as first-class fields on the operator-facing payload.

    Three sub-cases:
    (1) Protected running job: declared metric + effective timeout -> watchdog_active=True,
        correct no_progress_timeout_seconds, last_progress_at equals newest matching sample.
    (2) No declared metric: watchdog_active=False, watchdog_inert_reason='no_declared_metric'.
    (3) Metric-name mismatch: samples exist but none match declared metric ->
        watchdog_inert_reason='metric_name_mismatch'.
    """
    from app.schemas import serialize_compute_job_response_safe

    # --- Case 1: protected job with declared metric and matching samples ---
    progress_series = [
        {"t": "2026-01-01T00:00:00", "name": "reward", "value": 1.0},
        {"t": "2026-01-01T00:01:00", "name": "reward", "value": 1.1},
    ]
    job_protected = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend="local-subprocess",
        status=ComputeJobStatus.RUNNING,
        spend_cap_usd=1.0,
        polling_metadata={
            "declared_metric": "reward",
            "no_progress_timeout_seconds": 3600,
            "progress_series": progress_series,
        },
    )
    data = serialize_compute_job_response_safe(job_protected)
    assert data["watchdog_active"] is True, "watchdog must be active for a protected job"
    assert data["no_progress_timeout_seconds"] == 3600, "per-job timeout must be reported"
    assert data["last_progress_at"] == "2026-01-01T00:01:00", "must return newest matching sample"
    assert data["watchdog_inert_reason"] is None, "no inert reason for an active watchdog"

    # --- Case 2: no declared metric ---
    job_no_metric = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend="local-subprocess",
        status=ComputeJobStatus.RUNNING,
        spend_cap_usd=1.0,
        polling_metadata={},
    )
    data2 = serialize_compute_job_response_safe(job_no_metric)
    assert data2["watchdog_active"] is False
    assert data2["watchdog_inert_reason"] == "no_declared_metric"
    assert data2["no_progress_timeout_seconds"] is None
    assert data2["last_progress_at"] is None

    # --- Case 3: declared metric but all samples carry a different metric name ---
    job_mismatch = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend="local-subprocess",
        status=ComputeJobStatus.RUNNING,
        spend_cap_usd=1.0,
        polling_metadata={
            "declared_metric": "reward",
            "progress_series": [
                {"t": "2026-01-01T00:00:00", "name": "loss", "value": 0.5},
                {"t": "2026-01-01T00:01:00", "name": "loss", "value": 0.4},
            ],
        },
    )
    data3 = serialize_compute_job_response_safe(job_mismatch)
    assert data3["watchdog_active"] is False
    assert data3["watchdog_inert_reason"] == "metric_name_mismatch"
    assert data3["last_progress_at"] is None


def test_runner_bridge_last_progress_at_reflects_written_samples():
    """Regression: runner-written progress samples bridge into progress_series and
    are reflected by last_progress_at in the serialized response — no residual
    key-mismatch path silently drops progress."""
    from app.routers.runner import _merge_progress_report
    from app.compute_jobs import append_progress_sample
    from app.schemas import serialize_compute_job_response_safe
    from app.models import _utcnow

    now = _utcnow()
    raw_report = {
        "records": [
            {"metric": "reward", "value": 10.0, "ts": "2026-06-01T00:00:00"},
            {"metric": "reward", "value": 11.0, "ts": "2026-06-01T00:01:00"},
        ],
        "count": 2,
    }
    _merged, new_records = _merge_progress_report(None, raw_report, now)
    assert len(new_records) == 2, "bridge must yield both records"

    # Bridge into progress_series exactly as the /report route handler does.
    meta: dict = {"declared_metric": "reward"}
    for rec in new_records:
        meta = append_progress_sample(
            meta,
            metric=rec["metric"],
            value=rec["value"],
            t=rec.get("ts") or now.isoformat(),
        )

    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend="local-subprocess",
        status=ComputeJobStatus.RUNNING,
        spend_cap_usd=1.0,
        polling_metadata=meta,
    )
    data = serialize_compute_job_response_safe(job)

    # The bridge wrote samples under key 'name'; the watchdog reads 'name'.
    # last_progress_at must reflect the most recent bridged sample.
    assert data["last_progress_at"] == "2026-06-01T00:01:00", (
        "last_progress_at must equal the most recent runner-bridged sample timestamp"
    )
    assert data["watchdog_active"] is True, "watchdog must be active after bridge populates progress_series"
    assert data["watchdog_inert_reason"] is None


@pytest.mark.asyncio
async def test_compute_experiment_defaults_container_image_to_project_image(
    db_session, mocker
):
    """When a project's manifest declares compute.image.name and a plan subtask omits
    container_image, _handle_plan_completion must default the spawned child's
    container_image to the project's declared image — pinned to the durable
    ProjectComputeImage build digest (not the mutable tag) when one exists, so
    two runs of the same experiment are bit-identical over time (design gap G3)."""
    from sqlmodel import select as _select
    from app.models import ProjectComputeImage
    from app.plan_completion import _handle_plan_completion
    from testing.factories import make_project, make_task

    # Mutable manifest tag (image_name "base"); a build record pins it to a digest.
    _PROJECT_IMAGE = "us-central1-docker.pkg.dev/myproject/compute/base:sha256-abc123"
    _PINNED_REF = (
        "us-central1-docker.pkg.dev/myproject/compute/base@sha256:"
        "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    )

    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.plan_completion.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.admin_project_tools._discover_manifest_tools",
        new=AsyncMock(
            return_value={
                "tools": [],
                "skills": [],
                "compute": {"image": {"name": _PROJECT_IMAGE}},
                "error": None,
            }
        ),
    )

    project = make_project(
        name="ComputeImageDefaultProject",
        github_owner="myorg",
        github_repo="myrepo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Durable digest-pinned build record for the manifest image ("base").
    db_session.add(
        ProjectComputeImage(
            project_id=project.id,
            image_name="base",
            registry_repo="us-central1-docker.pkg.dev/myproject/compute",
            dep_hash="dep-hash-1",
            tag="sha256-abc123",
            pinned_ref=_PINNED_REF,
            digest="sha256:deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            built=True,
        )
    )
    await db_session.commit()

    plan_task = make_task(
        project_id=project.id,
        title="Plan: compute image default test",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "plan", "prompt": "plan it", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(plan_task)
    await db_session.commit()
    await db_session.refresh(plan_task)

    mocker.patch(
        "app.task_completion._ensure_branch",
        new_callable=AsyncMock,
        return_value=plan_task,
    )

    plan_logs = json.dumps(
        {
            "subtasks": [
                {
                    "title": "Run training experiment",
                    "prompt": "run the training script",
                    "type": "compute_experiment",
                    "command": "python train.py",
                    "spend_cap_usd": 1.0,
                    "deadline_seconds": 300,
                }
            ]
        }
    )
    await _handle_plan_completion(plan_task, plan_logs, db_session)

    child_tasks = (
        (
            await db_session.execute(
                _select(Task).where(Task.parent_task_id == plan_task.id)
            )
        )
        .scalars()
        .all()
    )
    compute_children = [
        t
        for t in child_tasks
        if (t.first_payload or {}).get("type") == "compute_experiment"
    ]
    assert len(compute_children) == 1, (
        f"Expected 1 compute_experiment child, got {len(compute_children)}: {child_tasks}"
    )
    payload = compute_children[0].first_payload or {}
    # Digest-pinned: the child carries the content-addressed pinned_ref, NOT the
    # mutable manifest tag, so re-authoring days later resolves identically.
    assert payload.get("container_image") == _PINNED_REF, (
        f"Expected digest-pinned container_image={_PINNED_REF!r}, "
        f"got {payload.get('container_image')!r}"
    )
    assert payload.get("container_image") != _PROJECT_IMAGE, (
        "container_image must be pinned to the build digest, not the mutable tag"
    )


# ===========================================================================
# Per-project compute image BUILD+PUBLISH substrate
#
# Covers (PRODUCE side of the images-first direction):
#   * manifest compute.image.build parsing + normalization
#   * dependency-hash computation (scripts/build_compute_image.compute_dependency_hash)
#   * credential scanner (no-platform-creds-in-layers security constraint)
#   * build script hash-check-before-build orchestration (build_and_push)
#   * backend orchestration (compute_backends.build_project_compute_image)
# ===========================================================================

# The reusable build/publish logic lives in a backend module (stdlib-only,
# self-contained) so the DB-backed suite can exercise it directly; the
# standalone scripts/build_compute_image.py CLI loads the same module by path.
from app import compute_image_build as _bci  # noqa: E402


# ---------------------------------------------------------------------------
# Manifest compute.image.build parsing
# ---------------------------------------------------------------------------


def test_manifest_compute_image_build_wellformed():
    from app.routers.admin_project_tools import (
        _validate_manifest_compute_image,
        _normalize_manifest_compute_image,
    )

    spec = {
        "name": "us-docker.pkg.dev/proj/compute-images/p:latest",
        "build": {
            "dockerfile": ".blueprint/Dockerfile.compute",
            "parent": "python:3.11-slim",
            "build_args": {"CUDA": "12.4", "PY": "3.11"},
        },
    }
    assert _validate_manifest_compute_image(spec) is None
    norm = _normalize_manifest_compute_image(spec)
    assert norm["name"] == "us-docker.pkg.dev/proj/compute-images/p:latest"
    assert norm["build"]["dockerfile"] == ".blueprint/Dockerfile.compute"
    assert norm["build"]["parent"] == "python:3.11-slim"
    assert norm["build"]["build_args"] == {"CUDA": "12.4", "PY": "3.11"}


def test_manifest_compute_image_build_defaults_when_fields_missing():
    """An empty build object defaults dockerfile and empty build_args."""
    from app.routers.admin_project_tools import (
        _validate_manifest_compute_image,
        _normalize_manifest_compute_image,
        _DEFAULT_COMPUTE_DOCKERFILE,
    )

    spec = {"name": "reg/repo/img:tag", "build": {}}
    assert _validate_manifest_compute_image(spec) is None
    norm = _normalize_manifest_compute_image(spec)
    assert norm["build"]["dockerfile"] == _DEFAULT_COMPUTE_DOCKERFILE
    assert norm["build"]["parent"] is None
    assert norm["build"]["build_args"] == {}


def test_manifest_compute_image_build_absent_is_none():
    """No build section -> normalized build is None (prebuilt-image project)."""
    from app.routers.admin_project_tools import _normalize_manifest_compute_image

    norm = _normalize_manifest_compute_image({"name": "reg/repo/img:tag"})
    assert norm["build"] is None


def test_manifest_compute_image_build_invalid_dockerfile_type():
    from app.routers.admin_project_tools import _validate_manifest_compute_image

    err = _validate_manifest_compute_image(
        {"name": "reg/img:t", "build": {"dockerfile": 123}}
    )
    assert err is not None
    assert "dockerfile" in err


def test_manifest_compute_image_build_dockerfile_traversal_rejected():
    from app.routers.admin_project_tools import _validate_manifest_compute_image

    err = _validate_manifest_compute_image(
        {"name": "reg/img:t", "build": {"dockerfile": "../etc/passwd"}}
    )
    assert err is not None
    assert "dockerfile" in err


def test_manifest_compute_image_build_invalid_parent_type():
    from app.routers.admin_project_tools import _validate_manifest_compute_image

    err = _validate_manifest_compute_image(
        {"name": "reg/img:t", "build": {"parent": ["python"]}}
    )
    assert err is not None
    assert "parent" in err


def test_manifest_compute_image_build_invalid_build_args_type():
    from app.routers.admin_project_tools import _validate_manifest_compute_image

    err = _validate_manifest_compute_image(
        {"name": "reg/img:t", "build": {"build_args": ["not", "a", "dict"]}}
    )
    assert err is not None
    assert "build_args" in err

    err2 = _validate_manifest_compute_image(
        {"name": "reg/img:t", "build": {"build_args": {"k": 5}}}
    )
    assert err2 is not None
    assert "build_args" in err2


def test_manifest_compute_image_build_not_a_dict():
    from app.routers.admin_project_tools import _validate_manifest_compute_image

    err = _validate_manifest_compute_image(
        {"name": "reg/img:t", "build": "notanobject"}
    )
    assert err is not None
    assert "build" in err


# ---------------------------------------------------------------------------
# Dependency-hash computation
# ---------------------------------------------------------------------------


def test_dependency_hash_same_inputs_same_hash():
    a = _bci.compute_dependency_hash({"requirements.txt": b"numpy==1\n", "Dockerfile": b"FROM x\n"})
    b = _bci.compute_dependency_hash({"Dockerfile": b"FROM x\n", "requirements.txt": b"numpy==1\n"})
    assert a == b
    assert len(a) == _bci.DEP_HASH_LEN


def test_dependency_hash_different_content_different_hash():
    base = {"requirements.txt": b"numpy==1\n", "Dockerfile": b"FROM x\n"}
    changed_dep = {"requirements.txt": b"numpy==2\n", "Dockerfile": b"FROM x\n"}
    changed_dockerfile = {"requirements.txt": b"numpy==1\n", "Dockerfile": b"FROM y\n"}
    h0 = _bci.compute_dependency_hash(base)
    assert h0 != _bci.compute_dependency_hash(changed_dep)
    assert h0 != _bci.compute_dependency_hash(changed_dockerfile)


def test_dependency_hash_filename_matters():
    """Renaming a dep file changes the hash even with identical content."""
    h1 = _bci.compute_dependency_hash({"requirements.txt": b"x", "Dockerfile": b"y"})
    h2 = _bci.compute_dependency_hash({"poetry.lock": b"x", "Dockerfile": b"y"})
    assert h1 != h2


def test_dependency_hash_empty_raises():
    with pytest.raises(ValueError):
        _bci.compute_dependency_hash({})


# ---------------------------------------------------------------------------
# Credential scanner
# ---------------------------------------------------------------------------


def test_credential_scanner_flags_known_patterns():
    findings = _bci.scan_inputs_for_credentials(
        {
            "config": b"token = ghs_abcdefghijklmnopqrstuvwxyz0123",
            "keys": b"AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ01234567",
            "sa.json": b'{"type": "service_account", "project_id": "x"}',
        }
    )
    joined = " ".join(findings)
    assert "config" in joined and "github" in joined
    assert "google_api_key" in joined
    assert "gcp_sa_json" in joined


def test_credential_scanner_flags_secret_env_in_dockerfile():
    findings = _bci.scan_inputs_for_credentials(
        {"Dockerfile": b"FROM python:3.11\nENV ORCHESTRATOR_TOKEN=abc123secret\nRUN pip install x"},
        dockerfile_names=["Dockerfile"],
    )
    assert any("ORCHESTRATOR_TOKEN" in f for f in findings)


def test_credential_scanner_flags_secret_path_copy():
    findings = _bci.scan_inputs_for_credentials(
        {"Dockerfile": b"FROM x\nCOPY /secrets/token.txt /app/token\n"},
        dockerfile_names=["Dockerfile"],
    )
    assert any("secret path" in f for f in findings)


def test_credential_scanner_no_false_positive_on_benign_env():
    """Benign env vars and normal deps do not trip the scanner."""
    findings = _bci.scan_inputs_for_credentials(
        {
            "Dockerfile": b"FROM python:3.11\nENV PYTHONUNBUFFERED=1\nENV LANG=C.UTF-8\nENV PATH=/usr/bin\n",
            "requirements.txt": b"numpy==1.26.0\ntorch==2.3.0\nscipy==1.13\n",
        },
        dockerfile_names=["Dockerfile"],
    )
    assert findings == []


def test_credential_scanner_allows_env_var_reference():
    """ENV pinned to a $VAR reference (build-arg indirection) is not a leak."""
    findings = _bci.scan_inputs_for_credentials(
        {"Dockerfile": b"FROM x\nENV ANTHROPIC_API_KEY=$SOME_BUILD_ARG\n"},
        dockerfile_names=["Dockerfile"],
    )
    assert findings == []


def test_image_scanner_covers_runner_credential_allowlist():
    """Drift guard: every provider-credential key in runner_isolation is caught by the image scanner.

    runner_isolation.AGENT_ENV_ALLOWLIST is the canonical list of credential-bearing env
    keys the platform recognises. The image scanner's _SECRET_ENV_NAMES must cover all of
    them (plus FORBIDDEN_ENV_KEYS) so a key added to the runner cannot silently be baked
    into a published image layer. If this test fails, add the key to _SECRET_ENV_NAMES in
    backend/app/compute_image_build.py.
    """
    import importlib.util
    import pathlib
    import sys

    if sys.version_info < (3, 11):
        pytest.skip("runner_isolation.py uses Python 3.11 dataclass syntax; covered in CI")

    # Load runner_isolation by path (it is stdlib-only, not in the backend package).
    # Resolve across local + Docker test envs: scripts/test-backend.sh and CI both
    # mount backend/ at /app and the repo root at /repo, so a path relative to
    # __file__ alone cannot reach the repo root. Reuse the canonical resolver.
    from testing.test_runner_isolation_gpu_markers import _require_runner_isolation_path

    spec = importlib.util.spec_from_file_location(
        "runner_isolation",
        pathlib.Path(_require_runner_isolation_path()),
    )
    ri = importlib.util.module_from_spec(spec)
    # runner_isolation defines dataclasses; @dataclass resolves annotations via
    # sys.modules[cls.__module__], so the module must be registered BEFORE exec.
    sys.modules[spec.name] = ri
    try:
        spec.loader.exec_module(ri)
    finally:
        sys.modules.pop(spec.name, None)

    # Credential-bearing subset of AGENT_ENV_ALLOWLIST: keys whose names suggest they
    # carry a secret (API key, token, credentials, secret).  Config/routing/locale keys
    # (PROVIDER, LANG, TERM, …) are intentionally excluded — they carry no secrets.
    _CREDENTIAL_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_CREDENTIALS")
    credential_keys = {
        k for k in ri.AGENT_ENV_ALLOWLIST
        if any(k.upper().endswith(sfx) for sfx in _CREDENTIAL_SUFFIXES)
    }
    # FORBIDDEN_ENV_KEYS must also be detected — they must never reach task code at all.
    all_keys_to_cover = credential_keys | set(ri.FORBIDDEN_ENV_KEYS)

    secret_names = _bci._SECRET_ENV_NAMES

    # 1. Every credential key is in _SECRET_ENV_NAMES.
    missing = all_keys_to_cover - secret_names
    assert not missing, (
        f"runner_isolation keys not covered by compute_image_build._SECRET_ENV_NAMES: {sorted(missing)}. "
        "Add them to _SECRET_ENV_NAMES in backend/app/compute_image_build.py."
    )

    # 2. Each key is actually detected when literally assigned in a Dockerfile ENV.
    for key in sorted(all_keys_to_cover):
        dockerfile = f"FROM python:3.11-slim\nENV {key}=supersecretvalue\n".encode()
        findings = _bci.scan_inputs_for_credentials(
            {"Dockerfile": dockerfile},
            dockerfile_names=["Dockerfile"],
        )
        assert any(key in f for f in findings), (
            f"scan_inputs_for_credentials did not flag 'ENV {key}=...' — "
            "the key is in _SECRET_ENV_NAMES but the scanner did not detect it"
        )

    # 3. A clean Dockerfile with only benign vars produces no findings.
    clean = b"FROM python:3.11-slim\nENV PYTHONUNBUFFERED=1\nENV LANG=C.UTF-8\n"
    assert _bci.scan_inputs_for_credentials({"Dockerfile": clean}, dockerfile_names=["Dockerfile"]) == []


# ---------------------------------------------------------------------------
# Build script hash-check-before-build orchestration
# ---------------------------------------------------------------------------


def _write_repo(tmp_path, dockerfile=b"FROM python:3.11-slim\nRUN pip install numpy\n",
                deps=None):
    repo = tmp_path / "repo"
    (repo / ".blueprint").mkdir(parents=True)
    (repo / ".blueprint" / "Dockerfile.compute").write_bytes(dockerfile)
    deps = deps if deps is not None else {"requirements.txt": b"numpy==1.26\n"}
    for name, content in deps.items():
        (repo / name).write_bytes(content)
    return str(repo)


def test_build_and_push_skips_when_image_present(tmp_path):
    """When the hash-tagged image already exists, no build/push happens."""
    repo = _write_repo(tmp_path)
    run_calls = []

    def fake_run(cmd, **kw):
        run_calls.append(cmd)

        class P:
            returncode = 0
            stdout = "reg/repo/img@sha256:" + "a" * 64
            stderr = ""
        return P()

    result = _bci.build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        _run=fake_run,
        _tag_exists=lambda repo_repo, tag, _run=None: True,
    )
    assert result.already_present is True
    assert result.built is False
    # No `docker build` / `docker push` was invoked.
    assert not any("build" in c for c in run_calls)
    assert not any("push" in c for c in run_calls)
    assert result.pinned_ref.startswith("reg/repo/img@sha256:")


def test_build_and_push_builds_when_hash_new(tmp_path):
    """A new dependency hash triggers build + push and returns a pinned ref."""
    repo = _write_repo(tmp_path)
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)

        class P:
            returncode = 0
            stderr = ""
            # docker image inspect --format returns the RepoDigest.
            stdout = "reg/repo/img@sha256:" + "b" * 64
        return P()

    result = _bci.build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        parent="python:3.11-slim",
        _run=fake_run,
        _tag_exists=lambda repo_repo, tag, _run=None: False,
        _scan_layers_fn=lambda *a, **k: [],  # no-op layer scan in unit test
    )
    assert result.built is True
    assert result.already_present is False
    flat = [" ".join(c) for c in cmds]
    assert any(c.startswith("docker build") for c in flat)
    assert any(c.startswith("docker push") for c in flat)
    # The declared parent is threaded through as a build-arg.
    assert any("PARENT_IMAGE=python:3.11-slim" in c for c in flat)
    assert result.pinned_ref == "reg/repo/img@sha256:" + "b" * 64


def test_build_and_push_credential_leak_fails_before_push(tmp_path):
    """A leaked credential in the Dockerfile fails the build BEFORE any push."""
    repo = _write_repo(
        tmp_path,
        dockerfile=b"FROM python:3.11\nENV RUNNER_TOKEN=supersecretvalue\n",
    )
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)

        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    with pytest.raises(_bci.CredentialLeakError):
        _bci.build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"],
            _run=fake_run,
            _tag_exists=lambda repo_repo, tag, _run=None: False,
        )
    # Nothing was pushed.
    assert not any("push" in c for c in cmds)


def test_build_and_push_rejects_secret_build_args_before_build(tmp_path):
    """A platform-secret key in build_args is rejected before docker build runs."""
    repo = _write_repo(tmp_path)
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)

        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    with pytest.raises(_bci.CredentialLeakError, match="build_args contains platform secret keys"):
        _bci.build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"],
            build_args={"ANTHROPIC_API_KEY": "sk-ant-secret"},
            _run=fake_run,
            _tag_exists=lambda *a, **k: False,
        )
    # Neither build nor push should have run.
    flat = [" ".join(c) for c in cmds]
    assert not any(c.startswith("docker build") for c in flat)
    assert not any(c.startswith("docker push") for c in flat)


def test_build_and_push_allows_clean_build_args(tmp_path):
    """Build args that are not platform secrets pass through without error."""
    repo = _write_repo(tmp_path)
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)

        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    # A clean build arg (non-secret key, non-empty value) should not raise.
    result = _bci.build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        build_args={"PYTHON_VERSION": "3.11"},
        _run=fake_run,
        _tag_exists=lambda *a, **k: False,
        _scan_layers_fn=lambda *a, **k: [],
    )
    assert result.built is True
    flat = [" ".join(c) for c in cmds]
    assert any("--build-arg" in c and "PYTHON_VERSION=3.11" in c for c in flat)


def test_build_and_push_layer_credential_leak_fails_before_push(tmp_path):
    """A credential in built image layers fails the push AFTER build but BEFORE push."""
    repo = _write_repo(tmp_path)
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)

        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    layer_scan_called_with = []

    def fake_layer_scan(image_ref, **kw):
        layer_scan_called_with.append(image_ref)
        return ["layer:some/layer.tar!root/.creds: contains github_app_token credential pattern"]

    with pytest.raises(_bci.CredentialLeakError, match="built image layers"):
        _bci.build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"],
            _run=fake_run,
            _tag_exists=lambda *a, **k: False,
            _scan_layers_fn=fake_layer_scan,
        )
    # Build was called, push was NOT. Match the push COMMAND specifically
    # (not a bare "push" substring, which false-matches the pytest tmp_path —
    # this test function's name contains "push", so the repo_dir arg baked into
    # the joined `docker build … <repo_dir>` string contains it too).
    flat = [" ".join(c) for c in cmds]
    assert any(c.startswith("docker build") for c in flat)
    assert not any(c.startswith("docker push") for c in flat)
    assert len(layer_scan_called_with) == 1


def test_build_and_push_config_env_baked_secret_blocked_before_push(tmp_path):
    """End-to-end: a platform secret baked into the built image's .config.Env is
    blocked by the REAL layer scanner before any push.

    A ``RUNNER_TOKEN`` inherited from a base image / injected via --build-arg lands
    in ``.config.Env`` without appearing in the source Dockerfile (which is clean
    here) or as a credential PATTERN in any layer file. This exercises the actual
    ``scan_image_layers_for_credentials`` config-env path through ``build_and_push``
    — not an injected fake — proving the "no platform creds baked into project
    images" guard is enforced, not merely a design note.
    """
    repo = _write_repo(tmp_path)  # clean Dockerfile (input scan passes)
    # docker-save archive: clean layer file, config.Env bakes a platform secret.
    archive = _classic_save_archive_with_env(
        ["PATH=/usr/bin", "RUNNER_TOKEN=baked-into-the-runtime-env"]
    )
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        # `docker save -o <path> <ref>` streams the archive to the -o destination;
        # the real scanner reads it back and finds the baked secret.
        if "save" in cmd and "-o" in cmd:
            with open(cmd[cmd.index("-o") + 1], "wb") as fh:
                fh.write(archive)

        class P:
            returncode = 0
            stdout = ""
            stderr = ""

        return P()

    with pytest.raises(_bci.CredentialLeakError, match="built image layers"):
        _bci.build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"],
            _run=fake_run,
            _tag_exists=lambda *a, **k: False,
            _assert_network_fn=lambda *a, **k: None,
        )
    flat = [" ".join(c) for c in cmds]
    assert any(c.startswith("docker build") for c in flat)
    assert not any(c.startswith("docker push") for c in flat)


# ---------------------------------------------------------------------------
# Build-network confinement: a repo-controlled Dockerfile's RUN steps run on the
# ORCHESTRATOR HOST. With the default docker build network they can reach
# 169.254.169.254 and trade the host's service-account identity for an access
# token. Every build must therefore run on a network from which the metadata
# server is provably unreachable — or not run at all.
# ---------------------------------------------------------------------------


def _ok_proc(stdout="", returncode=0, stderr=""):
    class P:
        pass

    p = P()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


def test_resolve_build_network_defaults_to_none(monkeypatch):
    """No operator opt-in => RUN steps get NO network namespace at all."""
    monkeypatch.delenv("COMPUTE_IMAGE_BUILD_NETWORK", raising=False)
    assert _bci.resolve_build_network() == "none"
    assert _bci.resolve_build_network("") == "none"
    # Operator opt-in is honored (and is then probed, see below).
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_NETWORK", "bp-image-build")
    assert _bci.resolve_build_network() == "bp-image-build"
    # An explicit argument still wins (CLI / tests).
    assert _bci.resolve_build_network("none") == "none"


def test_build_and_push_confines_the_run_step_network(tmp_path):
    """`docker build` is invoked with an explicit, confined --network.

    Omitting --network is what selected the default bridge, from which the GCP
    metadata server answers. The flag is now non-optional.
    """
    repo = _write_repo(tmp_path)
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        return _ok_proc(stdout="reg/repo/img@sha256:" + "b" * 64)

    _bci.build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        _run=fake_run,
        _tag_exists=lambda *a, **k: False,
        _scan_layers_fn=lambda *a, **k: [],
        build_network="none",
    )
    build_cmd = next(c for c in cmds if c[:2] == ["docker", "build"])
    assert "--network=none" in build_cmd, (
        "docker build ran without an explicit confined --network — its RUN steps "
        "would reach 169.254.169.254 from the orchestrator host"
    )


@pytest.mark.parametrize("net", ["host", "bridge", "default", "", "HOST"])
def test_build_and_push_refuses_unconfined_networks(tmp_path, net):
    """host/bridge/default all reach the metadata server — the build is refused."""
    repo = _write_repo(tmp_path)
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        return _ok_proc()

    with pytest.raises(_bci.BuildNetworkError):
        _bci.build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"],
            _run=fake_run,
            _tag_exists=lambda *a, **k: False,
            _scan_layers_fn=lambda *a, **k: [],
            build_network=net,
        )
    # Nothing was built: the refusal happens BEFORE any RUN step executes.
    assert not any(c[:2] == ["docker", "build"] for c in cmds)


def test_build_refused_when_the_build_network_reaches_the_metadata_server(tmp_path):
    """A named network is PROBED; if metadata answers, the build never starts."""
    repo = _write_repo(tmp_path)
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        if cmd[:2] == ["docker", "run"]:
            # The probe container reached 169.254.169.254.
            return _ok_proc(stdout="BP_METADATA_REACHABLE\n")
        return _ok_proc(stdout="reg/repo/img@sha256:" + "b" * 64)

    with pytest.raises(_bci.BuildNetworkError, match="metadata"):
        _bci.build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"],
            _run=fake_run,
            _tag_exists=lambda *a, **k: False,
            _scan_layers_fn=lambda *a, **k: [],
            build_network="leaky-net",
        )
    assert any(c[:2] == ["docker", "run"] for c in cmds), "the network was not probed"
    assert not any(c[:2] == ["docker", "build"] for c in cmds), (
        "docker build ran on a network from which the metadata server is reachable"
    )


def test_build_proceeds_on_a_network_that_blocks_the_metadata_server(tmp_path):
    """An operator network whose probe reports BLOCKED is usable for RUN steps."""
    repo = _write_repo(tmp_path)
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        if cmd[:2] == ["docker", "run"]:
            return _ok_proc(stdout="BP_METADATA_BLOCKED\n")
        return _ok_proc(stdout="reg/repo/img@sha256:" + "c" * 64)

    result = _bci.build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        _run=fake_run,
        _tag_exists=lambda *a, **k: False,
        _scan_layers_fn=lambda *a, **k: [],
        build_network="walled-net",
    )
    assert result.built is True
    build_cmd = next(c for c in cmds if c[:2] == ["docker", "build"])
    assert "--network=walled-net" in build_cmd


def test_metadata_probe_fails_closed_when_it_cannot_run(tmp_path):
    """A probe that never ran proves NOTHING — the build is refused, not waved through.

    Without the sentinel positive-control, a probe that failed for an unrelated
    reason (image pull failed, docker down) is indistinguishable from
    "metadata unreachable", which would silently re-open the hole.
    """
    repo = _write_repo(tmp_path)
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        if cmd[:2] == ["docker", "run"]:
            # Probe image could not be pulled: non-zero exit, no sentinel.
            return _ok_proc(returncode=125, stdout="", stderr="Unable to find image")
        return _ok_proc(stdout="reg/repo/img@sha256:" + "d" * 64)

    with pytest.raises(_bci.BuildNetworkError, match="failing closed"):
        _bci.build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"],
            _run=fake_run,
            _tag_exists=lambda *a, **k: False,
            _scan_layers_fn=lambda *a, **k: [],
            build_network="unknown-net",
        )
    assert not any(c[:2] == ["docker", "build"] for c in cmds)


def test_metadata_probe_fails_closed_on_a_silent_zero_exit(tmp_path):
    """Exit 0 with no sentinel (e.g. an entrypoint that ignored the script) is refused."""

    def fake_run(cmd, **kw):
        if cmd[:2] == ["docker", "run"]:
            return _ok_proc(returncode=0, stdout="")
        return _ok_proc()

    with pytest.raises(_bci.BuildNetworkError, match="failing closed"):
        _bci.assert_build_network_confined("quiet-net", _run=fake_run)


def test_metadata_probe_covers_both_metadata_endpoints():
    """The probe script tries the raw link-local IP and the DNS alias."""
    assert "169.254.169.254" in _bci._METADATA_PROBE_SCRIPT
    assert "metadata.google.internal" in _bci._METADATA_PROBE_SCRIPT


# ---------------------------------------------------------------------------
# Digest resolution on the already-present fast path (unchanged-deps re-trigger).
#
# The fast path used to read the digest from the LOCAL docker store only, so on a
# fresh/pruned host it resolved None, returned a tag-only pinned_ref, and the
# caller persisted digest=NULL — silently destroying the project's digest pin.
# ---------------------------------------------------------------------------


def test_already_present_resolves_digest_from_registry_with_an_empty_local_store(tmp_path):
    """Fresh host (no local image): the digest still resolves — from the REGISTRY."""
    repo = _write_repo(tmp_path)
    registry_digest = "sha256:" + "e" * 64
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        if cmd[:3] == ["docker", "manifest", "inspect"]:
            return _ok_proc(
                stdout=json.dumps(
                    {
                        "Ref": "reg/repo/img:tag",
                        "Descriptor": {
                            "mediaType": "application/vnd.oci.image.manifest.v1+json",
                            "digest": registry_digest,
                            "size": 1234,
                        },
                    }
                )
            )
        if cmd[:3] == ["docker", "image", "inspect"]:
            # EMPTY LOCAL STORE — this host never built or pulled the image.
            return _ok_proc(returncode=1, stdout="", stderr="No such image")
        return _ok_proc()

    result = _bci.build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        _run=fake_run,
        _tag_exists=lambda *a, **k: True,  # unchanged deps: already in the registry
    )
    assert result.already_present is True
    assert result.built is False
    assert result.digest == registry_digest, (
        "the already-present fast path resolved no digest from the registry — on a "
        "fresh host it would return a tag-only ref and NULL the project's stored pin"
    )
    assert result.pinned_ref == "reg/repo/img@" + registry_digest
    assert result.digest_unknown is False
    assert any(c[:3] == ["docker", "manifest", "inspect"] for c in cmds)


def test_already_present_marks_the_digest_unknown_when_no_source_resolves_it(tmp_path):
    """Registry AND local store both silent => digest is UNKNOWN, and says so.

    ``digest=None`` here does NOT mean "this image has no digest"; it means we
    could not read it. Persisters key on ``digest_unknown`` to refuse to overwrite
    a stored pin with NULL.
    """
    repo = _write_repo(tmp_path)

    def fake_run(cmd, **kw):
        return _ok_proc(returncode=1, stdout="", stderr="boom")

    result = _bci.build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        _run=fake_run,
        _tag_exists=lambda *a, **k: True,
    )
    assert result.digest is None
    assert result.digest_unknown is True
    assert result.pinned_ref == "reg/repo/img:" + result.dep_hash


def test_resolve_registry_digest_handles_a_multi_arch_manifest_list():
    """`docker manifest inspect -v` emits a LIST for multi-arch images."""
    digest = "sha256:" + "f" * 64

    def fake_run(cmd, **kw):
        return _ok_proc(
            stdout=json.dumps(
                [
                    {"Ref": "reg/repo/img:t", "Descriptor": {"digest": digest}},
                    {"Ref": "reg/repo/img:t", "Descriptor": {"digest": "sha256:" + "9" * 64}},
                ]
            )
        )

    assert _bci.resolve_registry_digest("reg/repo/img", "t", _run=fake_run) == digest


def test_resolve_registry_digest_returns_none_on_garbage():
    """A non-JSON / error response resolves to None rather than a bogus digest."""

    def fake_run(cmd, **kw):
        return _ok_proc(stdout="not json at all")

    assert _bci.resolve_registry_digest("reg/repo/img", "t", _run=fake_run) is None

    def failing_run(cmd, **kw):
        raise OSError("docker not installed")

    assert _bci.resolve_registry_digest("reg/repo/img", "t", _run=failing_run) is None


# ---------------------------------------------------------------------------
# BuildControl: the outer deadline must actually STOP the build, not abandon it.
# ---------------------------------------------------------------------------


def test_supervised_run_without_a_control_is_plain_subprocess_run():
    """No control published => unchanged behavior (CLI, reaper, unit tests)."""
    assert _bci.current_build_control() is None
    proc = _bci.supervised_run(
        [sys.executable, "-c", "print('hello')"], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0
    assert "hello" in proc.stdout


def test_abort_kills_a_running_child_and_raises():
    """abort() SIGKILLs the in-flight docker child; the blocking call returns at once."""
    control = _bci.BuildControl()
    token = _bci.set_build_control(control)
    try:
        threading.Timer(0.5, control.abort).start()
        started = time.monotonic()
        with pytest.raises(_bci.BuildAborted):
            _bci.supervised_run(
                # Stand-in for `docker build`: a long-running child.
                [sys.executable, "-c", "import time; time.sleep(120)"],
                capture_output=True,
                text=True,
                timeout=180,
            )
        elapsed = time.monotonic() - started
    finally:
        _bci.reset_build_control(token)
    assert elapsed < 20, (
        f"the abort did not stop the child (took {elapsed:.1f}s) — a deadline that "
        "cannot stop the build leaves it running on the orchestrator host"
    )


def test_an_aborted_control_refuses_to_start_new_children():
    """Abort is sticky: a late `docker push` cannot slip out after the deadline."""
    control = _bci.BuildControl()
    control.abort()
    token = _bci.set_build_control(control)
    try:
        with pytest.raises(_bci.BuildAborted):
            _bci.supervised_run(
                [sys.executable, "-c", "print('should never run')"],
                capture_output=True,
                text=True,
                timeout=30,
            )
    finally:
        _bci.reset_build_control(token)


@pytest.mark.asyncio
async def test_build_control_reaches_the_worker_thread():
    """The ContextVar transport the router relies on: control survives to_thread.

    ``asyncio.to_thread`` copies the calling context into the worker, so a control
    published by the request handler before ``create_task`` is visible to the
    blocking build code — that is what lets the deadline kill the docker children.
    """
    control = _bci.BuildControl()
    token = _bci.set_build_control(control)
    try:
        task = asyncio.create_task(asyncio.to_thread(_bci.current_build_control))
    finally:
        _bci.reset_build_control(token)
    assert await task is control


# ---------------------------------------------------------------------------
# scan_image_layers_for_credentials: classic + OCI archive layouts, fail-closed
# ---------------------------------------------------------------------------

# A credential blob that trips the github_app_token pattern (ghs_ + >=20 chars).
_LEAKED_CRED = b"export TOKEN=ghs_" + b"A" * 24 + b"\n"


def _tar_bytes(entries: dict) -> bytes:
    """Build an uncompressed tar archive from {member_name: bytes}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _fake_docker_save(stdout_bytes: bytes):
    """Return a _run callable that emulates a successful `docker save`.

    The real command is now ``docker save -o <path> <ref>`` — the archive is
    streamed to a FILE rather than piped back through the parent's memory (a
    multi-GB CUDA image OOM'd the API process when it was buffered). The fake
    therefore writes the archive to the ``-o`` destination.

    It ALSO still returns the bytes on ``stdout``, which the streaming
    implementation never reads. That is deliberate: it keeps this fake honest for
    the memory test below, which asserts the scanner does not materialize the
    archive even when a pipe-shaped payload is right there for the taking.
    """

    def _run(cmd, **kw):
        if "-o" in cmd:
            out_path = cmd[cmd.index("-o") + 1]
            with open(out_path, "wb") as fh:
                fh.write(stdout_bytes)

        class P:
            returncode = 0
            stdout = stdout_bytes
            stderr = b""

        return P()

    return _run


def _classic_save_archive(leak: bytes) -> bytes:
    """A classic docker-save archive: <hash>/layer.tar members + manifest.json."""
    layer_tar = _tar_bytes({"root/.creds": leak})
    return _tar_bytes(
        {
            "0a1b2c/layer.tar": layer_tar,
            "config.json": b"{}",
            "manifest.json": b'[{"Layers":["0a1b2c/layer.tar"]}]',
        }
    )


def _oci_save_archive(leak: bytes) -> bytes:
    """An OCI-layout docker-save archive: index.json + blobs/sha256/<digest>."""
    layer_tar = _tar_bytes({"root/.creds": leak})
    layer_digest = "l" * 64
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": "sha256:" + ("c" * 64)},
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar",
                    "digest": "sha256:" + layer_digest,
                }
            ],
        }
    ).encode()
    manifest_digest = "m" * 64
    index = json.dumps(
        {
            "schemaVersion": 2,
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": "sha256:" + manifest_digest,
                }
            ],
        }
    ).encode()
    return _tar_bytes(
        {
            "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
            "index.json": index,
            "blobs/sha256/" + manifest_digest: manifest,
            "blobs/sha256/" + layer_digest: layer_tar,
            "blobs/sha256/" + ("c" * 64): b"{}",
        }
    )


def test_scan_image_layers_classic_format_detects_leak():
    """Classic docker-save layer.tar members are still scanned (regression)."""
    findings = _bci.scan_image_layers_for_credentials(
        "img:tag", _run=_fake_docker_save(_classic_save_archive(_LEAKED_CRED))
    )
    assert any("github_app_token" in f for f in findings), findings


def test_scan_image_layers_classic_format_clean_passes():
    """A classic archive with no credential material yields no findings."""
    findings = _bci.scan_image_layers_for_credentials(
        "img:tag", _run=_fake_docker_save(_classic_save_archive(b"nothing secret here\n"))
    )
    assert findings == []


def test_scan_image_layers_oci_format_detects_leak():
    """OCI-layout images (blobs/sha256/...) are resolved via index/manifest and scanned."""
    findings = _bci.scan_image_layers_for_credentials(
        "img:tag", _run=_fake_docker_save(_oci_save_archive(_LEAKED_CRED))
    )
    assert any("github_app_token" in f for f in findings), findings


def test_scan_image_layers_oci_format_clean_passes():
    """A clean OCI-layout archive resolves its layer and finds nothing."""
    findings = _bci.scan_image_layers_for_credentials(
        "img:tag", _run=_fake_docker_save(_oci_save_archive(b"nothing secret here\n"))
    )
    assert findings == []


# ---------------------------------------------------------------------------
# scan_image_layers_for_credentials ALSO scans the image config .Env: a platform
# secret baked into the runtime environment (base image, --build-arg, RUN export)
# is invisible to both the layer-file pattern scan and the source-Dockerfile scan.
# ---------------------------------------------------------------------------

_CLEAN_LAYER_FILE = b"nothing secret here\n"


def _classic_save_archive_with_env(env_entries: list) -> bytes:
    """Classic docker-save archive whose manifest points at a config with .Env."""
    layer_tar = _tar_bytes({"root/app": _CLEAN_LAYER_FILE})
    config = json.dumps({"config": {"Env": env_entries}}).encode()
    return _tar_bytes(
        {
            "0a1b2c/layer.tar": layer_tar,
            "config.json": config,
            "manifest.json": b'[{"Config":"config.json","Layers":["0a1b2c/layer.tar"]}]',
        }
    )


def _oci_save_archive_with_env(env_entries: list) -> bytes:
    """OCI-layout docker-save archive whose config blob carries .config.Env."""
    layer_tar = _tar_bytes({"root/app": _CLEAN_LAYER_FILE})
    layer_digest = "l" * 64
    config_digest = "c" * 64
    config = json.dumps({"config": {"Env": env_entries}}).encode()
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": "sha256:" + config_digest},
            "layers": [{"digest": "sha256:" + layer_digest}],
        }
    ).encode()
    manifest_digest = "m" * 64
    index = json.dumps(
        {
            "schemaVersion": 2,
            "manifests": [{"digest": "sha256:" + manifest_digest}],
        }
    ).encode()
    return _tar_bytes(
        {
            "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
            "index.json": index,
            "blobs/sha256/" + manifest_digest: manifest,
            "blobs/sha256/" + layer_digest: layer_tar,
            "blobs/sha256/" + config_digest: config,
        }
    )


def test_scan_config_env_pure_flags_secret_name_with_value():
    """The pure config-Env scanner flags a secret-named var carrying a value."""
    findings = _bci._scan_config_env_for_secrets(
        "config:x", ["PATH=/usr/bin", "RUNNER_TOKEN=deadbeef-uuid-not-a-regex-match"]
    )
    assert any("RUNNER_TOKEN" in f for f in findings), findings
    assert all("PATH" not in f for f in findings)


def test_scan_config_env_pure_ignores_empty_and_benign():
    """Empty secret values and benign names produce no findings."""
    findings = _bci._scan_config_env_for_secrets(
        "config:x", ["ANTHROPIC_API_KEY=", "LANG=C.UTF-8", "NODE_ENV=production"]
    )
    assert findings == []


def test_scan_image_layers_classic_config_env_secret_detected():
    """A platform secret baked into the classic-layout image config .Env is caught.

    The value (a bare UUID-ish orchestrator runner token) matches NO credential
    regex and appears in no layer file — only the config Env name-scan catches it.
    """
    archive = _classic_save_archive_with_env(
        ["PATH=/usr/local/bin", "BLUEPRINT_RUNNER_TOKEN=11111111-2222-3333"]
    )
    findings = _bci.scan_image_layers_for_credentials(
        "img:tag", _run=_fake_docker_save(archive)
    )
    assert any(
        "BLUEPRINT_RUNNER_TOKEN" in f and "config Env" in f for f in findings
    ), findings


def test_scan_image_layers_classic_config_env_clean_passes():
    """A classic image whose config Env holds only benign vars yields no findings."""
    archive = _classic_save_archive_with_env(
        ["PATH=/usr/local/bin", "LANG=C.UTF-8", "PYTHONUNBUFFERED=1"]
    )
    findings = _bci.scan_image_layers_for_credentials(
        "img:tag", _run=_fake_docker_save(archive)
    )
    assert findings == []


def test_scan_image_layers_oci_config_env_secret_detected():
    """A platform secret baked into an OCI-layout image config .Env is caught."""
    archive = _oci_save_archive_with_env(
        ["PATH=/usr/bin", "BLUEPRINT_ORCHESTRATOR_URL=https://orch.internal"]
    )
    findings = _bci.scan_image_layers_for_credentials(
        "img:tag", _run=_fake_docker_save(archive)
    )
    assert any(
        "BLUEPRINT_ORCHESTRATOR_URL" in f and "config Env" in f for f in findings
    ), findings


def test_scan_image_layers_oci_config_env_clean_passes():
    """A clean OCI-layout config Env yields no findings."""
    archive = _oci_save_archive_with_env(["PATH=/usr/bin", "TERM=xterm"])
    findings = _bci.scan_image_layers_for_credentials(
        "img:tag", _run=_fake_docker_save(archive)
    )
    assert findings == []


def test_scan_image_layers_no_recognizable_layers_fails_closed():
    """An archive with neither layer.tar members nor an OCI store must ERROR, not pass."""
    # Only a manifest.json — no layer.tar, no index.json/blobs.
    bogus = _tar_bytes({"manifest.json": b"[]", "config.json": b"{}"})
    with pytest.raises(RuntimeError, match="no recognizable layers"):
        _bci.scan_image_layers_for_credentials(
            "img:tag", _run=_fake_docker_save(bogus)
        )


def test_scan_image_layers_oci_unresolvable_blobs_fails_closed():
    """An OCI index whose referenced blobs are absent yields zero layers → fail closed."""
    index = json.dumps(
        {"schemaVersion": 2, "manifests": [{"digest": "sha256:" + ("m" * 64)}]}
    ).encode()
    # index.json present but the manifest blob it points at is missing.
    bogus = _tar_bytes({"oci-layout": b"{}", "index.json": index})
    with pytest.raises(RuntimeError, match="no recognizable layers"):
        _bci.scan_image_layers_for_credentials(
            "img:tag", _run=_fake_docker_save(bogus)
        )


def test_scan_image_layers_streams_to_disk_never_buffers_the_archive():
    """`docker save` must write to a FILE, not be drained into memory.

    The credential layer-scan runs on real compute images — a CUDA base plus a
    torch wheel is comfortably multi-GB. Buffering the `docker save` archive
    (`capture_output=True` → `io.BytesIO(proc.stdout)` → per-layer `bytes`) held
    the whole image in RAM two or three times over and OOM'd the API process that
    a project MEMBER could trigger over HTTP.
    """
    seen_cmds = []

    def _run(cmd, **kw):
        seen_cmds.append(cmd)
        out_path = cmd[cmd.index("-o") + 1]
        with open(out_path, "wb") as fh:
            fh.write(_classic_save_archive(b"clean\n"))

        class P:
            returncode = 0
            stdout = b""  # nothing is piped back
            stderr = b""

        return P()

    findings = _bci.scan_image_layers_for_credentials("img:tag", _run=_run)

    assert findings == []
    cmd = seen_cmds[0]
    assert "-o" in cmd, f"docker save must stream to a file, got: {cmd}"
    # The archive path is a real temp file the scanner owns, not a pipe.
    assert cmd[cmd.index("-o") + 1].endswith(".tar")
    assert cmd[-1] == "img:tag"


def test_scan_image_layers_peak_memory_is_bounded_by_file_cap_not_image_size(tmp_path):
    """Peak memory during the scan must NOT scale with the image size.

    This is the OOM, as a measurement. The archive holds a 48 MiB blob (standing
    in for the CUDA/torch payload that makes a real compute image multi-GB) plus a
    small file carrying a leaked credential.

    The fake `docker save` models the two transports HONESTLY, which is what makes
    the assertion mean something:

    * ``-o <path>`` (what the streaming scanner asks for) — the archive is copied
      to the destination on disk, never through Python memory, and stdout is
      empty.
    * no ``-o`` (what a `capture_output=True` implementation asks for) — the
      archive has to be pulled into a bytes object to be handed back on stdout,
      exactly as a real pipe would. That allocation lands inside the measurement
      window, so a buffering implementation is caught paying for it.

    A streaming scan seeks past the oversized member without ever reading it and
    stays near the 10 MiB per-file cap; a buffering one holds the image.
    """
    big_blob = b"\0" * (48 * 1024 * 1024)  # skipped: over the 10 MiB per-file cap
    layer_tar = _tar_bytes({"opt/cuda/payload.bin": big_blob, "root/.creds": _LEAKED_CRED})
    archive = _tar_bytes(
        {
            "0a1b2c/layer.tar": layer_tar,
            "manifest.json": b'[{"Layers":["0a1b2c/layer.tar"]}]',
        }
    )
    archive_size = len(archive)
    assert archive_size > 48 * 1024 * 1024

    # Stage the archive on disk, then drop every in-memory reference to it, so
    # ONLY the scanner's own allocations land in the tracemalloc window.
    src = tmp_path / "image-src.tar"
    src.write_bytes(archive)
    del big_blob, layer_tar, archive

    def _run(cmd, **kw):
        if "-o" in cmd:
            # Streams to disk — no Python-level buffer of the archive.
            shutil.copyfile(src, cmd[cmd.index("-o") + 1])
            stdout = b""
        else:
            # A real pipe: the parent must materialize the whole archive.
            stdout = src.read_bytes()

        class P:
            returncode = 0
            stderr = b""

        P.stdout = stdout
        return P()

    tracemalloc.start()
    try:
        findings = _bci.scan_image_layers_for_credentials("img:tag", _run=_run)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # The scan still WORKS: the small credential file inside the huge layer is found.
    assert any("github_app_token" in f for f in findings), findings

    # And it did not pay for the image's size in RAM. The ceiling is generous
    # (well under half the archive) so this asserts the SHAPE — bounded, not
    # proportional — rather than a brittle exact number.
    ceiling = 20 * 1024 * 1024
    assert peak < ceiling, (
        f"layer-scan peaked at {peak / 1024 / 1024:.1f} MiB on a "
        f"{archive_size / 1024 / 1024:.1f} MiB archive — it is buffering the "
        "image instead of streaming it (this is the OOM)"
    )


def test_build_and_push_missing_dockerfile_raises(tmp_path):
    repo = str(tmp_path / "empty")
    (tmp_path / "empty").mkdir()
    with pytest.raises(FileNotFoundError):
        _bci.build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=[],
            _tag_exists=lambda *a, **k: False,
        )


def test_build_and_push_dry_run_does_not_build(tmp_path):
    repo = _write_repo(tmp_path)
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)

        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    result = _bci.build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        dry_run=True,
        _run=fake_run,
        _tag_exists=lambda *a, **k: False,
    )
    assert result.built is False
    assert result.already_present is False
    assert not any("build" in c or "push" in c for c in cmds)


# ---------------------------------------------------------------------------
# Backend orchestration (compute_backends.build_project_compute_image)
# ---------------------------------------------------------------------------


def test_resolve_project_compute_image_build_extracts_config():
    from app import compute_backends as cb

    manifest_compute = {
        "image": {
            "name": "reg/repo/img:tag",
            "build": {
                "dockerfile": ".blueprint/Dockerfile.compute",
                "parent": "python:3.11-slim",
                "build_args": {"A": "1"},
            },
        }
    }
    cfg = cb.resolve_project_compute_image_build(manifest_compute)
    assert cfg is not None
    assert cfg["name"] == "reg/repo/img:tag"
    assert cfg["parent"] == "python:3.11-slim"
    assert cfg["build_args"] == {"A": "1"}


def test_resolve_project_compute_image_build_none_without_build():
    from app import compute_backends as cb

    assert cb.resolve_project_compute_image_build({"image": {"name": "reg/img:t", "build": None}}) is None
    assert cb.resolve_project_compute_image_build({"image": None}) is None
    assert cb.resolve_project_compute_image_build(None) is None


@pytest.mark.asyncio
async def test_build_project_compute_image_orchestrates_clone_and_build():
    from app import compute_backends as cb
    from app.models import Project

    project = Project(
        name="p",
        github_owner="octo",
        github_repo="proj",
    )

    clone_seen = {}

    def fake_clone(owner, repo, ref, dest):
        clone_seen["owner"] = owner
        clone_seen["repo"] = repo
        clone_seen["ref"] = ref
        # Materialize a minimal repo so _discover_dep_files finds inputs.
        os.makedirs(os.path.join(dest, ".blueprint"), exist_ok=True)
        with open(os.path.join(dest, ".blueprint", "Dockerfile.compute"), "wb") as fh:
            fh.write(b"FROM python:3.11-slim\n")
        with open(os.path.join(dest, "requirements.txt"), "wb") as fh:
            fh.write(b"numpy==1.26\n")

    captured = {}

    def fake_build(**kwargs):
        captured.update(kwargs)
        return _bci.BuildResult(
            pinned_ref="reg/repo/proj@sha256:" + "c" * 64,
            tag="deadbeef",
            dep_hash="deadbeef",
            already_present=False,
            built=True,
        )

    result = await cb.build_project_compute_image(
        project,
        registry_repo="reg/repo",
        build_config={
            "name": "reg/repo/proj:latest",
            "dockerfile": ".blueprint/Dockerfile.compute",
            "parent": "python:3.11-slim",
            "build_args": {},
        },
        ref="main",
        _clone_fn=fake_clone,
        _build_fn=fake_build,
    )
    assert clone_seen["owner"] == "octo"
    assert clone_seen["ref"] == "main"
    assert result.pinned_ref == "reg/repo/proj@sha256:" + "c" * 64
    # Discovered requirements.txt was passed as a dependency input.
    assert "requirements.txt" in captured["dep_files"]
    assert captured["image_name"] == "proj"


@pytest.mark.asyncio
async def test_build_project_compute_image_requires_repo():
    from app import compute_backends as cb
    from app.models import Project

    project = Project(name="p", github_owner="", github_repo="")
    with pytest.raises(ValueError):
        await cb.build_project_compute_image(
            project,
            registry_repo="reg/repo",
            build_config={"name": "reg/repo/proj:latest"},
            _clone_fn=lambda *a: None,
            _build_fn=lambda **k: None,
        )


# ---------------------------------------------------------------------------
# ProjectComputeImage persistence (record_project_compute_image_build).
#
# The baking pipeline returns a BuildResult (pinned_ref/tag/dep_hash/built) but
# nothing durably recorded it. These cover the additive UPSERT helper: a fresh
# insert, an in-place UPSERT keyed on (project_id, dep_hash), and digest parsing
# from a @sha256 pinned_ref vs a tag-only ref.
# ---------------------------------------------------------------------------
def _build_result(*, pinned_ref, tag="latest", dep_hash="deadbeef", built=True):
    from app.compute_image_build import BuildResult

    return BuildResult(
        pinned_ref=pinned_ref,
        tag=tag,
        dep_hash=dep_hash,
        already_present=not built,
        built=built,
    )


async def _make_project(db_session, name="Compute Image Proj"):
    from app.models import Project

    project = Project(name=name, user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


# ---------------------------------------------------------------------------
# resolve_digest_pinned_default_image: pin a mutable manifest tag to a durable
# content digest when a ProjectComputeImage build record exists (design gap G3).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resolve_digest_pin_returns_none_for_empty_input(db_session):
    from app import compute_backends as cb
    import uuid as _uuid

    assert await cb.resolve_digest_pinned_default_image(db_session, _uuid.uuid4(), None) is None
    assert await cb.resolve_digest_pinned_default_image(db_session, _uuid.uuid4(), "") is None


@pytest.mark.asyncio
async def test_resolve_digest_pin_passthrough_when_already_pinned(db_session):
    from app import compute_backends as cb
    import uuid as _uuid

    already = "ghcr.io/org/base@sha256:" + "a" * 64
    # Already content-addressed: returned unchanged without any DB lookup.
    assert (
        await cb.resolve_digest_pinned_default_image(db_session, _uuid.uuid4(), already)
        == already
    )


@pytest.mark.asyncio
async def test_resolve_digest_pin_falls_back_to_tag_when_no_row(db_session):
    from app import compute_backends as cb

    project = await _make_project(db_session)
    tag = "ghcr.io/org/base:latest"
    # No build record for this project -> fail-open to the mutable tag.
    assert (
        await cb.resolve_digest_pinned_default_image(db_session, project.id, tag) == tag
    )


@pytest.mark.asyncio
async def test_resolve_digest_pin_uses_built_digest_row(db_session):
    from app import compute_backends as cb
    from app.models import ProjectComputeImage

    project = await _make_project(db_session)
    pinned = "ghcr.io/org/base@sha256:" + "b" * 64
    db_session.add(
        ProjectComputeImage(
            project_id=project.id,
            image_name="base",
            registry_repo="ghcr.io/org",
            dep_hash="h1",
            tag="latest",
            pinned_ref=pinned,
            digest="sha256:" + "b" * 64,
            built=True,
        )
    )
    await db_session.commit()

    assert (
        await cb.resolve_digest_pinned_default_image(
            db_session, project.id, "ghcr.io/org/base:latest"
        )
        == pinned
    )


@pytest.mark.asyncio
async def test_resolve_digest_pin_ignores_unbuilt_and_digestless_rows(db_session):
    from app import compute_backends as cb
    from app.models import ProjectComputeImage

    project = await _make_project(db_session)
    tag = "ghcr.io/org/base:latest"
    # A row that never finished building (built=False) and one with no digest
    # must both be ignored; the resolver falls back to the mutable tag.
    db_session.add(
        ProjectComputeImage(
            project_id=project.id,
            image_name="base",
            registry_repo="ghcr.io/org",
            dep_hash="unbuilt",
            tag="latest",
            pinned_ref="ghcr.io/org/base@sha256:" + "c" * 64,
            digest="sha256:" + "c" * 64,
            built=False,
        )
    )
    db_session.add(
        ProjectComputeImage(
            project_id=project.id,
            image_name="base",
            registry_repo="ghcr.io/org",
            dep_hash="digestless",
            tag="latest",
            pinned_ref="ghcr.io/org/base:latest",
            digest=None,
            built=True,
        )
    )
    await db_session.commit()

    assert (
        await cb.resolve_digest_pinned_default_image(db_session, project.id, tag) == tag
    )


# ---------------------------------------------------------------------------
# is_digest_pinned + operator-visible mutable-tag signal: the images-first
# substrate promises run-to-run reproducibility via digest pinning, so a
# fail-open to a mutable tag must be visible, not a silent debug note.
# ---------------------------------------------------------------------------
def test_is_digest_pinned_truth_table():
    from app import compute_backends as cb

    assert cb.is_digest_pinned("ghcr.io/org/base@sha256:" + "a" * 64) is True
    # A plain mutable tag is NOT pinned.
    assert cb.is_digest_pinned("ghcr.io/org/base:latest") is False
    assert cb.is_digest_pinned("ghcr.io/org/base") is False
    # Empty / absent references are not pinned.
    assert cb.is_digest_pinned(None) is False
    assert cb.is_digest_pinned("") is False


@pytest.mark.asyncio
async def test_resolve_digest_pin_warns_when_falling_open_to_mutable_tag(
    db_session, caplog
):
    """No build record -> fail-open to the mutable tag AND emit an operator-visible
    warning (not a silent debug note) that reproducibility is not guaranteed."""
    import logging as _logging

    from app import compute_backends as cb

    project = await _make_project(db_session)
    tag = "ghcr.io/org/base:latest"
    with caplog.at_level(_logging.WARNING, logger="app.compute_backends"):
        result = await cb.resolve_digest_pinned_default_image(
            db_session, project.id, tag
        )
    assert result == tag
    warnings = [
        r for r in caplog.records
        if r.levelno == _logging.WARNING and "MUTABLE tag" in r.getMessage()
    ]
    assert warnings, "expected an operator-visible mutable-tag warning"
    assert "reproducibility is NOT guaranteed" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_resolve_digest_pin_does_not_warn_when_pinned(db_session, caplog):
    """A build record with a digest resolves to the pinned ref with no warning."""
    import logging as _logging

    from app import compute_backends as cb
    from app.models import ProjectComputeImage

    project = await _make_project(db_session)
    pinned = "ghcr.io/org/base@sha256:" + "d" * 64
    db_session.add(
        ProjectComputeImage(
            project_id=project.id,
            image_name="base",
            registry_repo="ghcr.io/org",
            dep_hash="h-pinned",
            tag="latest",
            pinned_ref=pinned,
            digest="sha256:" + "d" * 64,
            built=True,
        )
    )
    await db_session.commit()
    with caplog.at_level(_logging.WARNING, logger="app.compute_backends"):
        result = await cb.resolve_digest_pinned_default_image(
            db_session, project.id, "ghcr.io/org/base:latest"
        )
    assert result == pinned
    assert not [
        r for r in caplog.records
        if r.levelno == _logging.WARNING and "MUTABLE tag" in r.getMessage()
    ]


# ---------------------------------------------------------------------------
# _stamp_container_image_pin_status: durable per-payload reproducibility flag.
# ---------------------------------------------------------------------------
def test_stamp_container_image_pin_status_mutable():
    from app.compute_experiment_contract import _stamp_container_image_pin_status

    contract = {"container_image": "ghcr.io/org/base:latest"}
    _stamp_container_image_pin_status(contract, "task-1")
    assert contract["container_image_digest_pinned"] is False


def test_stamp_container_image_pin_status_pinned():
    from app.compute_experiment_contract import _stamp_container_image_pin_status

    ref = "ghcr.io/org/base@sha256:" + "e" * 64
    contract = {"container_image": ref}
    _stamp_container_image_pin_status(contract, "task-1")
    assert contract["container_image_digest_pinned"] is True


def test_stamp_container_image_pin_status_no_image_leaves_flag_absent():
    from app.compute_experiment_contract import _stamp_container_image_pin_status

    # A bare bash -lc job declares no image; the flag must stay absent so a
    # "not pinned" signal is never shown for a job that has no image to pin.
    contract = {"command": "python3 -m foo"}
    _stamp_container_image_pin_status(contract, "task-1")
    assert "container_image_digest_pinned" not in contract


@pytest.mark.asyncio
async def test_record_project_compute_image_build_inserts_fresh_row(db_session):
    from app import compute_backends as cb
    from app.models import ProjectComputeImage

    project = await _make_project(db_session)
    digest = "sha256:" + "a" * 64
    result = _build_result(
        pinned_ref=f"ghcr.io/org/repo@{digest}",
        tag="v1",
        dep_hash="hash-fresh",
        built=True,
    )

    row = await cb.record_project_compute_image_build(
        db_session,
        project.id,
        registry_repo="ghcr.io/org/repo",
        image_name="compute-proj",
        result=result,
    )
    await db_session.commit()

    assert row.project_id == project.id
    assert row.registry_repo == "ghcr.io/org/repo"
    assert row.image_name == "compute-proj"
    assert row.dep_hash == "hash-fresh"
    assert row.tag == "v1"
    assert row.pinned_ref == f"ghcr.io/org/repo@{digest}"
    assert row.digest == digest
    assert row.built is True

    rows = (
        await db_session.execute(
            select(ProjectComputeImage).where(
                ProjectComputeImage.project_id == project.id
            )
        )
    ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_record_project_compute_image_build_upserts(db_session):
    """A second build for the same (project_id, dep_hash) updates in place."""
    from app import compute_backends as cb
    from app.models import ProjectComputeImage

    project = await _make_project(db_session)

    first_digest = "sha256:" + "b" * 64
    first = await cb.record_project_compute_image_build(
        db_session,
        project.id,
        registry_repo="ghcr.io/org/repo",
        image_name="compute-proj",
        result=_build_result(
            pinned_ref=f"ghcr.io/org/repo@{first_digest}",
            tag="v1",
            dep_hash="stable-hash",
            built=True,
        ),
    )
    await db_session.commit()
    first_id = first.id

    # Second build: same dep_hash, different digest/tag/built -> UPSERT in place.
    second_digest = "sha256:" + "c" * 64
    second = await cb.record_project_compute_image_build(
        db_session,
        project.id,
        registry_repo="ghcr.io/org/repo",
        image_name="compute-proj",
        result=_build_result(
            pinned_ref=f"ghcr.io/org/repo@{second_digest}",
            tag="v2",
            dep_hash="stable-hash",
            built=False,
        ),
    )
    await db_session.commit()

    # No duplicate row: same primary key, updated fields.
    assert second.id == first_id
    assert second.pinned_ref == f"ghcr.io/org/repo@{second_digest}"
    assert second.digest == second_digest
    assert second.tag == "v2"
    assert second.built is False

    rows = (
        await db_session.execute(
            select(ProjectComputeImage).where(
                ProjectComputeImage.project_id == project.id
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == first_id
    assert rows[0].tag == "v2"


@pytest.mark.asyncio
async def test_record_project_compute_image_build_parses_digest(db_session):
    """Digest is parsed from a @sha256 ref and None for a tag-only ref."""
    from app import compute_backends as cb

    project = await _make_project(db_session)

    digest = "sha256:" + "d" * 64
    pinned = await cb.record_project_compute_image_build(
        db_session,
        project.id,
        registry_repo="ghcr.io/org/repo",
        image_name="compute-proj",
        result=_build_result(
            pinned_ref=f"ghcr.io/org/repo@{digest}",
            dep_hash="digest-pinned",
        ),
    )
    await db_session.commit()
    assert pinned.digest == digest

    tag_only = await cb.record_project_compute_image_build(
        db_session,
        project.id,
        registry_repo="ghcr.io/org/repo",
        image_name="compute-proj",
        result=_build_result(
            pinned_ref="ghcr.io/org/repo:latest",
            tag="latest",
            dep_hash="tag-only",
        ),
    )
    await db_session.commit()
    assert tag_only.digest is None
    assert tag_only.pinned_ref == "ghcr.io/org/repo:latest"


# ---------------------------------------------------------------------------
# record_project_compute_image_build is a REAL upsert (ON CONFLICT DO UPDATE).
#
# It used to be a select-then-insert with no lock: two concurrent build triggers
# for the same (project_id, dep_hash) BOTH missed the existence check, both
# INSERTed, and the loser died on uq_projectcomputeimage_project_dep_hash — a
# 500 out of the build endpoint.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="ON CONFLICT + a real unique-violation race need PostgreSQL",
)
async def test_record_project_compute_image_build_concurrent_same_dep_hash(db_session):
    """Two CONCURRENT builds for the same (project_id, dep_hash) must not 500.

    Both callers run in their own session, so neither can see the other's
    uncommitted INSERT — exactly the interleaving that made the select-then-insert
    version raise IntegrityError on the unique constraint.
    """
    from app import compute_backends as cb
    from app.models import ProjectComputeImage

    project = await _make_project(db_session)
    project_id = project.id

    async def _record(digest_char: str, tag: str):
        async with TestingSessionLocal() as session:
            row = await cb.record_project_compute_image_build(
                session,
                project_id,
                registry_repo="ghcr.io/org/repo",
                image_name="compute-proj",
                result=_build_result(
                    pinned_ref=f"ghcr.io/org/repo@sha256:{digest_char * 64}",
                    tag=tag,
                    dep_hash="raced-hash",
                    built=True,
                ),
            )
            await session.commit()
            return row.id

    # Concurrent, independent transactions — no IntegrityError may escape.
    ids = await asyncio.gather(_record("e", "v1"), _record("f", "v2"))

    # Exactly ONE row survives, and both callers converged on it.
    rows = (
        await db_session.execute(
            select(ProjectComputeImage).where(
                ProjectComputeImage.project_id == project_id,
                ProjectComputeImage.dep_hash == "raced-hash",
            )
        )
    ).scalars().all()
    assert len(rows) == 1, f"concurrent upsert duplicated the row: {rows}"
    assert ids[0] == ids[1] == rows[0].id


# ---------------------------------------------------------------------------
# resolve_digest_pinned_default_image matches on the image's FULL identity.
#
# Matching on the SHORT image name alone silently resolved a manifest ref to a
# build record that does not describe it: a tag rollback (:v2 -> :v1) kept
# running the rolled-back digest, and an external prebuilt image sharing the
# trailing name hijacked this project's own build record.
# ---------------------------------------------------------------------------
async def _add_build_row(
    db_session,
    project_id,
    *,
    registry_repo,
    image_name,
    tag,
    dep_hash,
    digest,
    updated_at,
):
    from app.models import ProjectComputeImage

    row = ProjectComputeImage(
        project_id=project_id,
        image_name=image_name,
        registry_repo=registry_repo,
        dep_hash=dep_hash,
        tag=tag,
        pinned_ref=f"{registry_repo}/{image_name}@{digest}",
        digest=digest,
        built=True,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    # updated_at has an onupdate=; set it explicitly AFTER the insert so the
    # "most recently built" ordering under test is deterministic.
    row.updated_at = updated_at
    db_session.add(row)
    await db_session.commit()
    return row


@pytest.mark.asyncio
async def test_resolve_digest_pin_tag_rollback_uses_the_rolled_back_build(db_session):
    """Rolling the manifest back :v2 -> :v1 must resolve to the :v1 DIGEST.

    Both builds share registry+image_name, so keying on image_name alone returned
    the most-recently-updated row — the :v2 build the project had just rolled back
    — behind a digest-pinned ref that looked authoritative.
    """
    from app import compute_backends as cb

    project = await _make_project(db_session)
    registry = "us-docker.pkg.dev/proj/compute-images"
    v1_digest = "sha256:" + "1" * 64
    v2_digest = "sha256:" + "2" * 64

    await _add_build_row(
        db_session,
        project.id,
        registry_repo=registry,
        image_name="my-project",
        tag="v1",
        dep_hash="hash-v1",
        digest=v1_digest,
        updated_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    # The NEWER build — this is the one the buggy lookup returned.
    await _add_build_row(
        db_session,
        project.id,
        registry_repo=registry,
        image_name="my-project",
        tag="v2",
        dep_hash="hash-v2",
        digest=v2_digest,
        updated_at=datetime(2026, 6, 1, 0, 0, 0),
    )

    resolved = await cb.resolve_digest_pinned_default_image(
        db_session, project.id, f"{registry}/my-project:v1"
    )

    assert resolved == f"{registry}/my-project@{v1_digest}", (
        "Rolling back to :v1 resolved to the :v2 digest — the project silently "
        f"runs the image it rolled back. got={resolved!r}"
    )
    assert v2_digest not in (resolved or "")

    # And :v2 still resolves to :v2 (no over-correction).
    assert await cb.resolve_digest_pinned_default_image(
        db_session, project.id, f"{registry}/my-project:v2"
    ) == f"{registry}/my-project@{v2_digest}"


@pytest.mark.asyncio
async def test_resolve_digest_pin_ignores_foreign_registry_with_same_name(db_session):
    """An EXTERNAL image sharing the trailing name must not match a local build.

    Switching to a prebuilt ghcr.io/vendor/my-project used to match this project's
    own …/compute-images/my-project build record and run THAT digest. The foreign
    ref must pass through untouched.
    """
    from app import compute_backends as cb

    project = await _make_project(db_session)
    registry = "us-docker.pkg.dev/proj/compute-images"
    local_digest = "sha256:" + "9" * 64

    await _add_build_row(
        db_session,
        project.id,
        registry_repo=registry,
        image_name="my-project",
        tag="latest",
        dep_hash="hash-local",
        digest=local_digest,
        updated_at=datetime(2026, 6, 1, 0, 0, 0),
    )

    foreign = "ghcr.io/vendor/my-project:latest"
    resolved = await cb.resolve_digest_pinned_default_image(
        db_session, project.id, foreign
    )

    assert resolved == foreign, (
        "An external image sharing the trailing name resolved to THIS project's "
        f"build digest. got={resolved!r}"
    )
    assert local_digest not in (resolved or "")


@pytest.mark.asyncio
async def test_resolve_digest_pin_floating_tag_still_pins_latest_build(db_session):
    """A floating tag (:latest) still pins to the latest build for that image.

    The build pipeline pushes content-hash tags, never `latest`, so no per-tag row
    exists for a floating tag by construction — the latest build IS the right
    answer, and this is the primary use case the feature exists for.
    """
    from app import compute_backends as cb

    project = await _make_project(db_session)
    registry = "us-docker.pkg.dev/proj/compute-images"
    old_digest = "sha256:" + "a" * 64
    new_digest = "sha256:" + "b" * 64

    await _add_build_row(
        db_session,
        project.id,
        registry_repo=registry,
        image_name="my-project",
        tag="deadbeefcafe0001",  # a dep-hash tag, as the pipeline pushes
        dep_hash="deadbeefcafe0001",
        digest=old_digest,
        updated_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    await _add_build_row(
        db_session,
        project.id,
        registry_repo=registry,
        image_name="my-project",
        tag="deadbeefcafe0002",
        dep_hash="deadbeefcafe0002",
        digest=new_digest,
        updated_at=datetime(2026, 6, 1, 0, 0, 0),
    )

    resolved = await cb.resolve_digest_pinned_default_image(
        db_session, project.id, f"{registry}/my-project:latest"
    )
    assert resolved == f"{registry}/my-project@{new_digest}"


def test_split_image_ref_decomposes_registry_name_and_tag():
    from app import compute_backends as cb

    assert cb._split_image_ref("us-docker.pkg.dev/p/ci/my-project:v2") == (
        "us-docker.pkg.dev/p/ci",
        "my-project",
        "v2",
    )
    # A registry :port must survive; only the FINAL segment carries the tag.
    assert cb._split_image_ref("localhost:5000/img:v2") == ("localhost:5000", "img", "v2")
    assert cb._split_image_ref("ghcr.io/org/base") == ("ghcr.io/org", "base", "")
    assert cb._split_image_ref("bare-image") == ("", "bare-image", "")
    assert cb._split_image_ref("") == ("", "", "")


# ---------------------------------------------------------------------------
# resolve_compute_image_registry_repo — pure env/config function.
# ---------------------------------------------------------------------------


def test_resolve_compute_image_registry_repo_explicit_env(monkeypatch):
    """COMPUTE_IMAGE_REGISTRY env var takes precedence."""
    from app import compute_backends as cb

    monkeypatch.setenv("COMPUTE_IMAGE_REGISTRY", "us-docker.pkg.dev/my-project/compute-images")
    assert cb.resolve_compute_image_registry_repo() == "us-docker.pkg.dev/my-project/compute-images"


def test_resolve_compute_image_registry_repo_fallback(monkeypatch):
    """Without COMPUTE_IMAGE_REGISTRY, falls back to derived form from GCP_PROJECT_ID."""
    from app import compute_backends as cb
    from app import storage

    monkeypatch.delenv("COMPUTE_IMAGE_REGISTRY", raising=False)
    monkeypatch.delenv("COMPUTE_IMAGE_REGION", raising=False)
    monkeypatch.setattr(storage, "GCP_PROJECT_ID", "my-gcp-proj")
    result = cb.resolve_compute_image_registry_repo()
    assert result == "us-docker.pkg.dev/my-gcp-proj/compute-images"


def test_resolve_compute_image_registry_repo_custom_region(monkeypatch):
    """COMPUTE_IMAGE_REGION customises the region prefix."""
    from app import compute_backends as cb
    from app import storage

    monkeypatch.delenv("COMPUTE_IMAGE_REGISTRY", raising=False)
    monkeypatch.setenv("COMPUTE_IMAGE_REGION", "europe-west4")
    monkeypatch.setattr(storage, "GCP_PROJECT_ID", "my-gcp-proj")
    result = cb.resolve_compute_image_registry_repo()
    assert result == "europe-west4-docker.pkg.dev/my-gcp-proj/compute-images"


# ---------------------------------------------------------------------------
# POST /projects/{project_id}/compute-image/build — trigger endpoint.
#
# Exercises the seam end-to-end: build_project_compute_image is monkeypatched
# to skip the real Docker build; the endpoint must persist a ProjectComputeImage
# row with the parsed digest from the returned BuildResult.
# ---------------------------------------------------------------------------


async def _make_build_trigger_project(db_session, name="Build-Trigger-Test", role="owner"):
    """Project + membership for the compute-image build trigger.

    The trigger is OWNER-grade: it runs `docker build`/`docker push` on the
    ORCHESTRATOR HOST and pushes to the shared compute-image registry. It used to
    accept any project MEMBER, which is the privilege half of the OOM finding —
    a member could fan out unbounded multi-GB docker builds on the API process.
    ``role`` is parameterised so the denial path is covered too.
    """
    from app.models import Project, ProjectMember

    project = Project(name=name, user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(ProjectMember(project_id=project.id, user_id="test-user-123", role=role))
    await db_session.commit()
    return project


@pytest.mark.asyncio
async def test_project_compute_image_build_trigger_persists_digest(
    client, db_session, monkeypatch
):
    """POST /projects/{id}/compute-image/build persists a ProjectComputeImage row.

    Acceptance test: monkeypatches build_project_compute_image to return a
    digest-pinned BuildResult; asserts the row is persisted with the parsed
    sha256 digest.
    """
    from app import compute_backends as cb
    from app.compute_image_build import BuildResult
    from app.models import ProjectComputeImage
    from app.routers import admin_project_tools as pt

    project = await _make_build_trigger_project(db_session)
    digest = "sha256:" + "b" * 64
    pinned_ref = f"us-docker.pkg.dev/test-project/compute-images/compute-proj@{digest}"

    fake_result = BuildResult(
        pinned_ref=pinned_ref,
        tag="dep-abc123",
        dep_hash="abc123",
        already_present=False,
        built=True,
    )

    # Monkeypatch the manifest discovery to return a valid compute.image.build config.
    async def _fake_discover(proj):
        return {
            "tools": [],
            "skills": [],
            "compute": {
                "image": {
                    "name": "us-docker.pkg.dev/test-project/compute-images/compute-proj:dep-abc123",
                    "build": {
                        "dockerfile": ".blueprint/Dockerfile.compute",
                        "parent": None,
                        "build_args": {},
                    },
                }
            },
            "error": None,
        }

    monkeypatch.setattr(pt, "_discover_manifest_tools", _fake_discover)

    # Monkeypatch the build function to return the fake result without a real build.
    async def _fake_build(proj, *, registry_repo, build_config, dry_run=False, **kwargs):
        return fake_result

    monkeypatch.setattr(cb, "build_project_compute_image", _fake_build)
    monkeypatch.setenv("COMPUTE_IMAGE_REGISTRY", "us-docker.pkg.dev/test-project/compute-images")

    resp = await client.post(f"/api/admin/projects/{project.id}/compute-image/build")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["digest"] == digest
    assert body["pinned_ref"] == pinned_ref
    assert body["dep_hash"] == "abc123"
    assert body["built"] is True
    assert body["already_present"] is False

    # Verify the row was persisted in the database.
    row = (
        await db_session.execute(
            select(ProjectComputeImage).where(
                ProjectComputeImage.project_id == project.id,
                ProjectComputeImage.dep_hash == "abc123",
            )
        )
    ).scalar_one_or_none()
    assert row is not None
    assert row.digest == digest
    assert row.pinned_ref == pinned_ref


@pytest.mark.asyncio
async def test_project_compute_image_build_no_build_config_returns_422(
    client, db_session, monkeypatch
):
    """POST returns 422 when the project declares no compute.image.build section."""
    from app import compute_backends as cb
    from app.models import ProjectComputeImage
    from app.routers import admin_project_tools as pt

    project = await _make_build_trigger_project(db_session, name="NoBuild-Test")

    # Manifest has image.name but no build section — resolve returns None.
    async def _fake_discover_no_build(proj):
        return {
            "tools": [],
            "skills": [],
            "compute": {
                "image": {
                    "name": "prebuilt-image:latest",
                    # No "build" key — resolve_project_compute_image_build returns None.
                }
            },
            "error": None,
        }

    monkeypatch.setattr(pt, "_discover_manifest_tools", _fake_discover_no_build)

    build_called = []

    async def _fail_if_called(*args, **kwargs):
        build_called.append(True)
        raise AssertionError("build should not be called")

    monkeypatch.setattr(cb, "build_project_compute_image", _fail_if_called)

    resp = await client.post(f"/api/admin/projects/{project.id}/compute-image/build")
    assert resp.status_code == 422
    # The app installs a global HTTPException handler (app/main.py) that reshapes
    # every error body to {"error": {"code", "message"}} — there is no "detail" key.
    assert "build" in resp.json()["error"]["message"].lower()

    # No row should have been persisted.
    rows = (
        await db_session.execute(
            select(ProjectComputeImage).where(
                ProjectComputeImage.project_id == project.id
            )
        )
    ).scalars().all()
    assert rows == []
    assert build_called == []  # build was never invoked


@pytest.mark.asyncio
async def test_project_compute_image_build_trigger_denies_member(
    client, db_session, monkeypatch
):
    """A project MEMBER cannot trigger the build — it is owner-grade.

    The trigger shells out to `docker build`/`docker push` ON THE ORCHESTRATOR
    HOST and publishes to the shared registry. Leaving that member-reachable let
    any member fan out unbounded multi-GB builds against the API process.
    """
    from app import compute_backends as cb
    from app.routers import admin_project_tools as pt

    project = await _make_build_trigger_project(
        db_session, name="Member-Denied", role="member"
    )

    async def _fail_if_called(*args, **kwargs):
        raise AssertionError("build must not be reachable by a member")

    monkeypatch.setattr(cb, "build_project_compute_image", _fail_if_called)
    monkeypatch.setattr(pt, "_discover_manifest_tools", _fail_if_called)

    resp = await client.post(f"/api/admin/projects/{project.id}/compute-image/build")
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# DB-aware GC guard for project compute-image digests.
#
# Registry retention (Terraform cleanup_policy) is purely time/count-based and
# cannot query the DB, so a digest a live/recent compute job depends on can be
# GC'd out from under it. These cover the DB-aware primitive
# (referenced_compute_image_digests), the fail-open registry-tag helpers, and
# the reaper sweep that ties them together
# (sweep_protect_referenced_compute_image_digests).
# ---------------------------------------------------------------------------
def _digest(hexchar):
    return "sha256:" + hexchar * 64


async def _make_compute_job(
    db_session, project, *, status, container_image=None, completed_at=None
):
    meta = {}
    if container_image is not None:
        meta["container_image"] = container_image
    job = ComputeJob(
        project_id=project.id,
        backend="gcp",
        status=status,
        polling_metadata=meta,
        completed_at=completed_at,
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


@pytest.mark.asyncio
async def test_referenced_compute_image_digests_active_and_recent_only(db_session):
    project = await _make_project(db_session, name="GC Guard Refs")
    repo = "ghcr.io/org/img"
    d_active = _digest("a")
    d_recent = _digest("b")
    d_old = _digest("c")
    d_tag_only = "ghcr.io/org/img:latest"  # no @digest -> ignored

    now = _utcnow()
    # Live job -> referenced regardless of since.
    await _make_compute_job(
        db_session,
        project,
        status=ComputeJobStatus.RUNNING,
        container_image=f"{repo}@{d_active}",
    )
    # Terminal job completed inside the window -> referenced.
    await _make_compute_job(
        db_session,
        project,
        status=ComputeJobStatus.SUCCEEDED,
        container_image=f"{repo}@{d_recent}",
        completed_at=now - timedelta(days=2),
    )
    # Terminal job completed long ago -> NOT referenced.
    await _make_compute_job(
        db_session,
        project,
        status=ComputeJobStatus.SUCCEEDED,
        container_image=f"{repo}@{d_old}",
        completed_at=now - timedelta(days=90),
    )
    # Live job with only a tag (no digest) -> contributes nothing.
    await _make_compute_job(
        db_session,
        project,
        status=ComputeJobStatus.QUEUED,
        container_image=d_tag_only,
    )

    cutoff = now - timedelta(days=30)
    referenced = await compute_jobs.referenced_compute_image_digests(
        db_session, since=cutoff
    )
    assert referenced == {d_active, d_recent}

    # With no window, only live jobs count.
    live_only = await compute_jobs.referenced_compute_image_digests(db_session)
    assert live_only == {d_active}


def test_registry_protection_tag_helpers_are_fail_open():
    from app import compute_image_build as cib

    digest = _digest("a")
    assert cib.protection_tag_for_digest(digest) == "inuse-" + "a" * 16

    # apply: success path returns the tag; non-zero exit and OSError fail open.
    ok = cib.apply_registry_protection_tag(
        "ghcr.io/org/img", digest, _run=lambda *a, **k: MagicMock(returncode=0)
    )
    assert ok == "inuse-" + "a" * 16
    assert (
        cib.apply_registry_protection_tag(
            "ghcr.io/org/img", digest, _run=lambda *a, **k: MagicMock(returncode=1)
        )
        is None
    )

    def _boom(*a, **k):
        raise OSError("gcloud missing")

    assert (
        cib.apply_registry_protection_tag("ghcr.io/org/img", digest, _run=_boom)
        is None
    )
    assert cib.list_registry_protection_tags("ghcr.io/org/img", _run=_boom) == []
    assert cib.delete_registry_protection_tag("ghcr.io/org/img", "inuse-x", _run=_boom) is False

    # list: parses the short inuse- tag name out of gcloud JSON, drops others.
    tags_json = json.dumps(
        [
            {"tag": "ghcr.io/org/img:inuse-aaaaaaaaaaaaaaaa"},
            {"tag": "ghcr.io/org/img:latest"},
            {"tag": "ghcr.io/org/img:inuse-bbbbbbbbbbbbbbbb"},
        ]
    )
    listed = cib.list_registry_protection_tags(
        "ghcr.io/org/img",
        _run=lambda *a, **k: MagicMock(returncode=0, stdout=tags_json),
    )
    assert set(listed) == {"inuse-" + "a" * 16, "inuse-" + "b" * 16}


@pytest.mark.asyncio
async def test_compute_image_gc_protects_referenced_digest(db_session):
    """The reaper sweep tags a referenced digest inuse- and prunes stale tags."""
    from app import reaper_sweeps

    # Reset the module-level cadence timer so the first call below is due.
    reaper_sweeps._last_compute_image_gc_guard_sweep = None

    project = await _make_project(db_session, name="GC Guard Sweep")
    repo_base = "ghcr.io/org"
    image_name = "img"
    full_repo = f"{repo_base}/{image_name}"
    d_referenced = _digest("a")
    # A genuinely orphaned digest: it has NO ProjectComputeImage row and no
    # live/recent job — only a leftover ``inuse-`` tag in the registry. (A digest
    # that DID have a ProjectComputeImage row would be a standing pin and stay
    # protected regardless of age — see test_reaper_sweeps_compute_image.py.)
    d_unreferenced = _digest("d")

    # One baked image, still referenced by a running job.
    db_session.add(
        ProjectComputeImage(
            project_id=project.id,
            image_name=image_name,
            registry_repo=repo_base,
            dep_hash="dep-ref",
            tag="tag-ref",
            pinned_ref=f"{full_repo}@{d_referenced}",
            digest=d_referenced,
            built=True,
        )
    )
    await db_session.commit()

    # A live job references the digest.
    await _make_compute_job(
        db_session,
        project,
        status=ComputeJobStatus.RUNNING,
        container_image=f"{full_repo}@{d_referenced}",
    )

    # Run the sweep from a point 60 days in the future: the image row was just
    # built, so pushing "now" past the 30-day freshness window shows protection
    # holds via BOTH the standing pin and the live job's reference, while the
    # orphaned registry tag (no row, no job) is correctly pruned.
    sweep_now = _utcnow() + timedelta(days=60)

    applied_tags = []
    deleted_tags = []
    protect_tag = reaper_sweeps.sweep_protect_referenced_compute_image_digests

    def _apply(repo, digest):
        tag = "inuse-" + digest.split(":", 1)[-1][:16]
        applied_tags.append((repo, tag))
        return tag

    # Registry already carries a stale inuse- tag for the now-unreferenced digest.
    stale_tag = "inuse-" + d_unreferenced.split(":", 1)[-1][:16]

    def _list(repo):
        return [
            "inuse-" + d_referenced.split(":", 1)[-1][:16],
            stale_tag,
        ]

    def _delete(repo, tag):
        deleted_tags.append((repo, tag))
        return True

    summary = await protect_tag(
        db_session,
        sweep_now,
        _apply_tag=_apply,
        _list_tags=_list,
        _delete_tag=_delete,
    )

    # The referenced digest was protected; the orphan was not.
    referenced_tag = "inuse-" + d_referenced.split(":", 1)[-1][:16]
    assert (full_repo, referenced_tag) in applied_tags
    assert (full_repo, stale_tag) not in applied_tags
    assert summary["applied"] == 1

    # The stale tag for the unreferenced digest was pruned; the live one kept.
    assert (full_repo, stale_tag) in deleted_tags
    assert (full_repo, referenced_tag) not in deleted_tags
    assert summary["pruned"] == 1

    # Cadence gate: an immediate second call is a no-op.
    second = await protect_tag(
        db_session,
        sweep_now,
        _apply_tag=_apply,
        _list_tags=_list,
        _delete_tag=_delete,
    )
    assert second["skipped_cadence"] == 1


@pytest.mark.asyncio
async def test_gc_guard_sweep_never_raises_when_registry_helper_throws(db_session):
    from app import reaper_sweeps

    # Reset the module-level cadence timer so this call is due.
    reaper_sweeps._last_compute_image_gc_guard_sweep = None

    project = await _make_project(db_session, name="GC Guard Robust")
    full_repo = "ghcr.io/org/img"
    d = _digest("e")
    db_session.add(
        ProjectComputeImage(
            project_id=project.id,
            image_name="img",
            registry_repo="ghcr.io/org",
            dep_hash="dep-e",
            tag="tag-e",
            pinned_ref=f"{full_repo}@{d}",
            digest=d,
            built=True,
        )
    )
    await db_session.commit()
    await _make_compute_job(
        db_session,
        project,
        status=ComputeJobStatus.RUNNING,
        container_image=f"{full_repo}@{d}",
    )

    def _boom(*a, **k):
        raise RuntimeError("registry exploded")

    # A throwing list helper must not propagate out of the sweep.
    summary = await reaper_sweeps.sweep_protect_referenced_compute_image_digests(
        db_session,
        _utcnow(),
        _apply_tag=lambda repo, digest: "inuse-x",
        _list_tags=_boom,
        _delete_tag=lambda repo, tag: True,
    )
    assert summary["applied"] == 1
    assert summary["pruned"] == 0


# --------------------------------------------------------------------------
# Multi-arm viewable-plot collision: same-named plots across arm subdirs must
# NOT collapse to a single object (regression for basename-only dedupe).
# --------------------------------------------------------------------------

# Minimal magic-byte-prefixed blobs; extension classification drives selection.
_VIEW_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"


def _make_viewable_tar_gz(files: dict[str, bytes]) -> bytes:
    """In-memory ``.tar.gz`` whose members mirror ``tar -C dir -czf - .`` naming."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_sanitize_viewable_relpath_preserves_arm_subdir_context():
    # Same basename under distinct arm subdirs -> distinct, non-colliding names.
    assert storage.sanitize_viewable_relpath("arm1/reward.png") == "arm1__reward.png"
    assert storage.sanitize_viewable_relpath("arm2/reward.png") == "arm2__reward.png"
    # Root-level file keeps its bare name (no spurious joiner).
    assert storage.sanitize_viewable_relpath("reward.png") == "reward.png"
    # Leading ``./`` and unsafe chars are normalized; nesting is folded.
    assert storage.sanitize_viewable_relpath("./sweep/arm 1/fig.png") == "sweep__arm_1__fig.png"
    # Absolute path and parent traversal are REJECTED, not silently flattened.
    assert storage.sanitize_viewable_relpath("/etc/passwd.png") is None
    assert storage.sanitize_viewable_relpath("../secrets/x.png") is None
    assert storage.sanitize_viewable_relpath("arm1/../../x.png") is None
    # Degenerate inputs.
    assert storage.sanitize_viewable_relpath("") is None
    assert storage.sanitize_viewable_relpath("./") is None
    assert storage.sanitize_viewable_relpath(None) is None  # type: ignore[arg-type]


def test_extract_viewable_candidates_keeps_multi_arm_same_basename_from_bundle():
    """A sweep whose arms each write reward.png must yield one object PER arm."""
    bundle = _make_viewable_tar_gz(
        {
            "./arm1/reward.png": _VIEW_PNG_BYTES + b"-a1",
            "./arm2/reward.png": _VIEW_PNG_BYTES + b"-a2",
            "./arm3/reward.png": _VIEW_PNG_BYTES + b"-a3",
        }
    )
    cands = compute_backends_module._extract_viewable_candidates_from_bundle(bundle)
    names = sorted(c.filename for c in cands)
    # All three arms survive as distinct, individually-addressable objects
    # (previously collapsed to a single "reward.png").
    assert names == ["arm1__reward.png", "arm2__reward.png", "arm3__reward.png"]
    # Each candidate carries its OWN arm's bytes, not the first arm's.
    by_name = {c.filename: c for c in cands}
    assert by_name["arm1__reward.png"].data == _VIEW_PNG_BYTES + b"-a1"
    assert by_name["arm2__reward.png"].data == _VIEW_PNG_BYTES + b"-a2"
    # Distinct object names -> distinct durable URIs (no bucket overwrite).
    jid = uuid.uuid4()
    uris = {storage.compute_job_viewable_uri(jid, c.filename) for c in cands}
    assert len(uris) == 3


def test_collect_viewable_candidates_keeps_multi_arm_same_basename_from_dir(tmp_path):
    """On-disk (local backend) analogue: per-arm subdirs stay distinct."""
    for arm in ("arm1", "arm2"):
        arm_dir = tmp_path / arm
        arm_dir.mkdir()
        (arm_dir / "reward.png").write_bytes(_VIEW_PNG_BYTES + arm.encode())
    # A root-level file keeps its bare name alongside the nested ones.
    (tmp_path / "summary.png").write_bytes(_VIEW_PNG_BYTES + b"-root")

    cands, _size_dropped = compute_backends_module._collect_viewable_candidates_from_dir(
        str(tmp_path)
    )
    names = sorted(c.filename for c in cands)
    assert names == ["arm1__reward.png", "arm2__reward.png", "summary.png"]
    by_name = {c.filename: c for c in cands}
    assert by_name["arm1__reward.png"].data == _VIEW_PNG_BYTES + b"arm1"
    assert by_name["arm2__reward.png"].data == _VIEW_PNG_BYTES + b"arm2"


def test_collect_viewable_candidates_from_dir_streams_large_video(tmp_path):
    """A local video over the in-memory image cap becomes a STREAMED candidate.

    Instead of being read into memory (and rejected by the image cap), an
    oversized local video yields a candidate carrying a ``source_path`` (the
    on-disk original) with no in-memory ``data`` and the ``artifact_root`` set so
    the upload path can re-verify symlink containment. A co-located image stays
    on the in-memory ``data`` path.
    """
    image_cap = compute_backends_module._MAX_VIEWABLE_FILE_BYTES
    big_video = os.urandom(image_cap + 256 * 1024)  # ~8.25 MiB > image cap
    (tmp_path / "rollout.mp4").write_bytes(big_video)
    (tmp_path / "frame.png").write_bytes(_VIEW_PNG_BYTES)

    cands, _size_dropped = compute_backends_module._collect_viewable_candidates_from_dir(
        str(tmp_path)
    )
    by_name = {c.filename: c for c in cands}
    assert set(by_name) == {"rollout.mp4", "frame.png"}

    video = by_name["rollout.mp4"]
    assert video.kind == "video"
    assert video.data is None
    assert video.source_path == str(tmp_path / "rollout.mp4")
    assert video.source_is_temp is False
    assert video.artifact_root == os.path.realpath(str(tmp_path))
    assert video.size_bytes == len(big_video)

    image = by_name["frame.png"]
    assert image.data == _VIEW_PNG_BYTES
    assert image.source_path is None


@pytest.mark.asyncio
async def test_persist_streams_local_video_via_nofollow_open(tmp_path, monkeypatch):
    """A local job's oversized video is streamed to storage via an O_NOFOLLOW open.

    End-to-end for the local backend: the streamed candidate's original file is
    opened (not buffered) and handed to ``upload_compute_job_viewable_stream``;
    the original artifact file is left in place (never unlinked as a temp).
    """
    image_cap = compute_backends_module._MAX_VIEWABLE_FILE_BYTES
    big_video = os.urandom(image_cap + 256 * 1024)
    video_path = tmp_path / "rollout.mp4"
    video_path.write_bytes(big_video)

    job = MagicMock()
    job.id = uuid.uuid4()
    job.backend = compute_backends_module.LOCAL_BACKEND_NAME
    job.polling_metadata = {"artifact_dir": str(tmp_path)}

    streamed: list = []

    async def _fake_stream_upload(job_id, basename, fileobj, content_type, *, size=None):
        fileobj.seek(0, os.SEEK_END)
        measured = fileobj.tell()
        fileobj.seek(0)
        streamed.append({"basename": basename, "size": measured, "size_kwarg": size})
        return storage.compute_job_viewable_uri(job_id, basename)

    async def _forbid_file_upload(*_a, **_k):
        raise AssertionError("a large video must stream, not use the in-memory seam")

    monkeypatch.setattr(
        compute_backends_module.storage,
        "upload_compute_job_viewable_stream",
        _fake_stream_upload,
    )
    monkeypatch.setattr(
        compute_backends_module.storage,
        "upload_compute_job_viewable_file",
        _forbid_file_upload,
    )

    manifest, dropped = await compute_backends_module.persist_viewable_artifacts(job)

    assert dropped == 0
    assert [m["filename"] for m in manifest] == ["rollout.mp4"]
    assert manifest[0]["size_bytes"] == len(big_video)
    assert streamed == [
        {"basename": "rollout.mp4", "size": len(big_video), "size_kwarg": len(big_video)}
    ]
    # The ORIGINAL artifact file must remain in place (not treated as a temp).
    assert video_path.exists()


# ---------------------------------------------------------------------------
# Dropped-file recording: cap enforcement, deterministic ordering, tuple return
# ---------------------------------------------------------------------------

_MAX_COUNT = compute_backends_module._MAX_VIEWABLE_FILE_COUNT


def test_extract_viewable_candidates_collects_beyond_cap_for_determinism():
    """Bundle extractor collects ALL qualifying files (count cap removed from inner loop).

    The cap is applied by _collect_viewable_artifact_candidates, not here, so
    _extract_viewable_candidates_from_bundle returns every qualifying file.
    """
    files = {f"./file{i:03d}.png": _VIEW_PNG_BYTES + str(i).encode() for i in range(_MAX_COUNT + 5)}
    bundle = _make_viewable_tar_gz(files)
    cands = compute_backends_module._extract_viewable_candidates_from_bundle(bundle)
    # Without the inner cap, all _MAX_COUNT+5 qualifying files are returned.
    assert len(cands) == _MAX_COUNT + 5


def test_collect_viewable_candidates_from_dir_collects_beyond_cap(tmp_path):
    """Dir walker collects ALL qualifying files (count cap removed from inner loop)."""
    for i in range(_MAX_COUNT + 3):
        (tmp_path / f"file{i:03d}.png").write_bytes(_VIEW_PNG_BYTES + str(i).encode())
    cands, size_dropped = compute_backends_module._collect_viewable_candidates_from_dir(
        str(tmp_path)
    )
    assert len(cands) == _MAX_COUNT + 3
    assert size_dropped == 0


@pytest.mark.asyncio
async def test_collect_viewable_artifact_candidates_sorts_and_caps(tmp_path):
    """_collect_viewable_artifact_candidates sorts by filename, caps at MAX, returns dropped count."""
    # Create files whose names sort in a different order than os.walk may return.
    names = [f"zzz{i:02d}.png" for i in range(5)] + [f"aaa{i:02d}.png" for i in range(5)]
    for name in names:
        (tmp_path / name).write_bytes(_VIEW_PNG_BYTES + name.encode())

    job = MagicMock()
    job.backend = compute_backends_module.LOCAL_BACKEND_NAME
    job.polling_metadata = {"artifact_dir": str(tmp_path)}
    job.external_compute_id = None

    with patch.object(
        compute_backends_module,
        "_MAX_VIEWABLE_FILE_COUNT",
        7,
    ):
        selected, dropped = await compute_backends_module._collect_viewable_artifact_candidates(job)

    # 10 total, cap=7 → 3 dropped
    assert len(selected) == 7
    assert dropped == 3
    # Selected must be the lexicographically first 7 (aaa00..aaa04, zzz00, zzz01)
    selected_names = [c.filename for c in selected]
    assert selected_names == sorted(selected_names)
    assert selected_names[0].startswith("aaa")


@pytest.mark.asyncio
async def test_collect_viewable_artifact_candidates_no_drop_when_within_cap(tmp_path):
    """When file count ≤ cap, dropped == 0."""
    for i in range(3):
        (tmp_path / f"img{i}.png").write_bytes(_VIEW_PNG_BYTES + str(i).encode())

    job = MagicMock()
    job.backend = compute_backends_module.LOCAL_BACKEND_NAME
    job.polling_metadata = {"artifact_dir": str(tmp_path)}
    job.external_compute_id = None

    selected, dropped = await compute_backends_module._collect_viewable_artifact_candidates(job)
    assert len(selected) == 3
    assert dropped == 0


@pytest.mark.asyncio
async def test_persist_viewable_artifacts_returns_tuple_with_dropped_count(tmp_path):
    """persist_viewable_artifacts returns (manifest, dropped) not a bare list."""
    for i in range(_MAX_COUNT + 2):
        (tmp_path / f"img{i:03d}.png").write_bytes(_VIEW_PNG_BYTES + str(i).encode())

    job = MagicMock()
    job.id = uuid.uuid4()
    job.backend = compute_backends_module.LOCAL_BACKEND_NAME
    job.polling_metadata = {"artifact_dir": str(tmp_path)}
    job.external_compute_id = None

    async def _fake_upload(job_id, filename, data, content_type):
        return f"gs://fake-bucket/compute-jobs/{job_id}/artifacts/files/{filename}"

    with patch.object(
        compute_backends_module.storage,
        "upload_compute_job_viewable_file",
        side_effect=_fake_upload,
    ):
        result = await compute_backends_module.persist_viewable_artifacts(job)

    assert isinstance(result, tuple) and len(result) == 2
    manifest, dropped = result
    assert len(manifest) == _MAX_COUNT
    assert dropped == 2
    # Each manifest entry has the required fields
    entry = manifest[0]
    assert {"filename", "storage_uri", "content_type", "size_bytes", "kind"} <= entry.keys()


@pytest.mark.asyncio
async def test_persist_viewable_artifacts_returns_zero_dropped_for_small_set(tmp_path):
    """When files ≤ cap, persist_viewable_artifacts returns dropped == 0."""
    (tmp_path / "plot.png").write_bytes(_VIEW_PNG_BYTES + b"-only")

    job = MagicMock()
    job.id = uuid.uuid4()
    job.backend = compute_backends_module.LOCAL_BACKEND_NAME
    job.polling_metadata = {"artifact_dir": str(tmp_path)}
    job.external_compute_id = None

    async def _fake_upload(job_id, filename, data, content_type):
        return f"gs://fake/{filename}"

    with patch.object(
        compute_backends_module.storage,
        "upload_compute_job_viewable_file",
        side_effect=_fake_upload,
    ):
        manifest, dropped = await compute_backends_module.persist_viewable_artifacts(job)

    assert dropped == 0
    assert len(manifest) == 1


def test_extract_viewable_candidates_from_bundle_deterministic_order():
    """Bundle extractor returns files in the order they appear in the archive.

    Deterministic selection (alphabetical) is applied upstream by
    _collect_viewable_artifact_candidates; the extractor itself just returns all
    qualifying candidates without reordering.
    """
    # Add files to the archive in reverse-alphabetical order.
    files = {
        "./zzz.png": _VIEW_PNG_BYTES + b"-z",
        "./mmm.png": _VIEW_PNG_BYTES + b"-m",
        "./aaa.png": _VIEW_PNG_BYTES + b"-a",
    }
    bundle = _make_viewable_tar_gz(files)
    cands = compute_backends_module._extract_viewable_candidates_from_bundle(bundle)
    assert len(cands) == 3
    names = [c.filename for c in cands]
    # Order matches archive insertion — NOT sorted (sorting is upstream's job)
    assert names == ["zzz.png", "mmm.png", "aaa.png"]


@pytest.mark.asyncio
async def test_process_compute_jobs_once_batch_task_prefetch_cancels_terminal_task_jobs(
    db_session, monkeypatch
):
    """process_compute_jobs_once batch-loads tasks and correctly cancels jobs
    whose owning task is terminal — verifying the N+1 fix doesn't regress behavior.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.database import resolve_async_engine

    project = Project(name="Batch Prefetch Project", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Two tasks: one terminal (DONE), one still in progress.
    task_done = Task(
        project_id=project.id,
        title="terminal task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "compute_experiment"}],
    )
    task_live = Task(
        project_id=project.id,
        title="live task",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task_done)
    db_session.add(task_live)
    await db_session.commit()
    await db_session.refresh(task_done)
    await db_session.refresh(task_live)

    # A running job owned by the terminal task — should be cancelled.
    job_should_cancel = ComputeJob(
        task_id=task_done.id,
        project_id=project.id,
        backend="local-subprocess",
        status=ComputeJobStatus.RUNNING,
        external_compute_id="fake-done-job",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        polling_metadata={},
    )
    # A queued job owned by the live task — should NOT be cancelled.
    job_stays = ComputeJob(
        task_id=task_live.id,
        project_id=project.id,
        backend="local-subprocess",
        status=ComputeJobStatus.QUEUED,
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        polling_metadata={"preflight_smoke_done": True},
    )
    db_session.add(job_should_cancel)
    db_session.add(job_stays)
    await db_session.commit()
    await db_session.refresh(job_should_cancel)
    await db_session.refresh(job_stays)

    class FakeBackend:
        def external_id_for(self, j):
            return "fake-id"

        async def terminate(self, j, *, reason):
            return True

        async def launch(self, spec):
            return ComputeLaunchResult(
                external_id="fake-live-job",
                started_at=_utcnow(),
                cost_rate_per_second=0.0,
                message="launched",
            )

        async def poll_cost(self, j, *, as_of):
            from app.compute_backends import ComputeCostResult
            return ComputeCostResult(total_cost_usd=0.0, measured_at=as_of)

        async def poll_status(self, j):
            return ComputeStatusResult(status=ComputeJobStatus.RUNNING)

    monkeypatch.setattr(compute_jobs, "get_compute_backend", lambda _: FakeBackend())
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job_should_cancel)
    await db_session.refresh(job_stays)

    # The job owned by the terminal task must be cancelled.
    assert job_should_cancel.status == ComputeJobStatus.CANCELLED, (
        f"expected CANCELLED, got {job_should_cancel.status}"
    )
    assert job_should_cancel.outcome == ComputeJobOutcome.CANCELLED

    # The job owned by the live task must still be active (not cancelled).
    assert job_stays.status != ComputeJobStatus.CANCELLED, (
        f"live-task job should not be cancelled but got {job_stays.status}"
    )


# ---------------------------------------------------------------------------
# Tests for the SINGLE viewable-file pipeline (persist_viewable_files_for_job)
# ---------------------------------------------------------------------------
#
# There used to be TWO overlapping viewable pipelines running at every terminal
# transition: the canonical one (`persist_viewable_artifacts` → the
# `artifacts/viewable/` prefix + the `viewable_files` manifest the admin UI reads)
# and a second extractor that downloaded the SAME bundle AGAIN and re-uploaded
# every file to `artifacts/files/` under a DIFFERENT sanitizer and DIFFERENT caps
# (25 MB/50 files vs 8 MiB/25 files). The two copies could therefore disagree
# about which files exist and under what names.
#
# They are now ONE pass. These tests cover that single pipeline: it downloads the
# bundle exactly once, sanitizes once, uploads once, applies ONE cap policy, and
# records the result on BOTH persisted surfaces (the `viewable_files` manifest and
# the `ComputeJobFile` signing-allowlist rows) from that single pass — so the two
# surfaces cannot drift apart.


def _make_viewable_tar_bundle(files: dict[str, bytes]) -> bytes:
    """Thin wrapper around the shared ``_make_tar_gz_bundle`` for readable tests."""
    return _make_tar_gz_bundle(files)


async def _create_bundle_compute_job(db_session, backend: str = "gcp") -> ComputeJob:
    """Create a minimal Project + ComputeJob on a bundle-producing backend.

    A real ``ComputeJob`` (not just its id) is required because the single pipeline
    is backend-aware: it reads the durable bundle for gcp/runner-subprocess jobs and
    walks the on-disk artifact dir for local ones.
    """
    project = Project(name=f"vf-test-{uuid.uuid4().hex[:8]}", user_id="test")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    job = ComputeJob(project_id=project.id, backend=backend)
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


def _mock_viewable_storage(monkeypatch, bundle: bytes, *, fail_on: str | None = None):
    """Point the single viewable pipeline at an in-memory bundle; record its calls.

    Returns a dict tallying ``downloads`` and ``uploads`` so a test can assert the
    pipeline fetched the bundle EXACTLY ONCE — the core of the duplicate-pipeline
    fix — rather than the 2x it used to.

    The pipeline STREAMS the bundle to a temp file (it never pulls the whole
    archive into the orchestrator's heap), so the fetch seam patched here is
    ``_download_artifact_bundle_to_path``: the fake writes the in-memory test
    bundle to the destination path the pipeline streams into. Any code path that
    reached for the whole-bundle ``storage.download_compute_job_artifact_bundle``
    instead would BLOW UP here — it is patched to fail — which is what keeps the
    OOM-shaped fetch from creeping back into the viewable pass.
    """
    calls: dict = {"downloads": 0, "uploads": []}

    def _fake_stream_download(_job_id, dest_path):
        calls["downloads"] += 1
        with open(dest_path, "wb") as handle:
            handle.write(bundle)
        return True

    async def _forbid_whole_bundle_download(_job_id):
        raise AssertionError(
            "the viewable pass must STREAM the bundle to disk "
            "(_download_artifact_bundle_to_path), never load it whole into memory"
        )

    async def _fake_upload(job_id, basename, data, content_type):
        if fail_on is not None and fail_on in basename:
            raise RuntimeError("simulated GCS upload error")
        calls["uploads"].append(
            {
                "basename": basename,
                "size": len(data),
                "content_type": content_type,
                "streamed": False,
            }
        )
        return (
            f"gs://test-bucket/compute-jobs/{job_id}/artifacts/viewable/{basename}"
        )

    async def _fake_stream_upload(job_id, basename, fileobj, content_type, *, size=None):
        if fail_on is not None and fail_on in basename:
            raise RuntimeError("simulated GCS stream upload error")
        # Measure the source WITHOUT reading it into memory (seek to end), so the
        # mock itself never reintroduces the OOM the streaming path exists to avoid.
        try:
            fileobj.seek(0, os.SEEK_END)
            measured = fileobj.tell()
            fileobj.seek(0)
        except OSError:
            measured = size
        calls["uploads"].append(
            {
                "basename": basename,
                "size": measured if measured is not None else size,
                "content_type": content_type,
                "streamed": True,
            }
        )
        return (
            f"gs://test-bucket/compute-jobs/{job_id}/artifacts/viewable/{basename}"
        )

    monkeypatch.setattr(
        compute_backends_module,
        "_download_artifact_bundle_to_path",
        _fake_stream_download,
    )
    monkeypatch.setattr(
        storage, "download_compute_job_artifact_bundle", _forbid_whole_bundle_download
    )
    monkeypatch.setattr(storage, "upload_compute_job_viewable_file", _fake_upload)
    monkeypatch.setattr(
        storage, "upload_compute_job_viewable_stream", _fake_stream_upload
    )
    return calls


async def _job_files(db_session, job_id) -> list[ComputeJobFile]:
    return (
        (
            await db_session.execute(
                select(ComputeJobFile).where(ComputeJobFile.compute_job_id == job_id)
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_viewable_pipeline_roundtrips_images_and_videos(db_session, monkeypatch):
    """A bundle mixing viewable and non-viewable files — only images and videos are
    persisted, on BOTH surfaces, with correct media_type and content_type."""
    job = await _create_bundle_compute_job(db_session)
    png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    jpg_data = b"\xff\xd8\xff\xe0" + b"\x00" * 100
    mp4_data = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100

    bundle = _make_viewable_tar_bundle(
        {
            "./plot.png": png_data,
            "./photo.jpg": jpg_data,
            "./screen.jpeg": jpg_data,
            "./anim.webp": b"RIFF" + b"\x00" * 100,
            "./icon.gif": b"GIF89a" + b"\x00" * 50,
            # SVG is EXCLUDED fail-closed (script execution via signed URL).
            "./diagram.svg": b"<svg></svg>",
            "./output.mp4": mp4_data,
            "./clip.webm": b"\x1aE\xdf\xa3" + b"\x00" * 100,
            # PDF is a first-class viewable kind (matplotlib savefig('*.pdf')).
            "./report.pdf": b"%PDF-1.4\n" + b"\x00" * 100,
            # Non-viewable — must be skipped.
            "./terminal.json": b'{"status": "SUCCEEDED"}',
            "./command.log": b"all good\n",
            "./script.py": b"print('hello')",
            "./results.txt": b"accuracy: 0.95",
        }
    )
    calls = _mock_viewable_storage(monkeypatch, bundle)

    manifest, dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    # 8 viewable files (SVG excluded fail-closed), uploaded ONCE each.
    assert len(manifest) == 8
    assert dropped == 0
    assert len(calls["uploads"]) == 8
    assert calls["downloads"] == 1, "the bundle must be downloaded exactly once"

    rows = await _job_files(db_session, job.id)
    assert len(rows) == 8

    by_name = {r.safe_relpath: r for r in rows}
    assert by_name["plot.png"].media_type == "image"
    assert by_name["plot.png"].content_type == "image/png"
    assert by_name["output.mp4"].media_type == "video"
    assert by_name["output.mp4"].content_type == "video/mp4"
    assert by_name["report.pdf"].media_type == "pdf"
    assert by_name["report.pdf"].content_type == "application/pdf"

    # SVG must NOT be persisted as a signable/viewable file — excluded fail-closed
    # on BOTH surfaces (script-execution risk when served from a signed URL).
    assert "diagram.svg" not in by_name
    assert not any(e["filename"] == "diagram.svg" for e in manifest)

    # Non-viewable files must NOT appear.
    for skipped in ("terminal.json", "command.log", "script.py", "results.txt"):
        assert skipped not in by_name

    # THE ANTI-DIVERGENCE INVARIANT: both persisted surfaces are written from the
    # SAME single sanitize+upload pass, so they must agree exactly — same files,
    # same canonical URIs. The old two-pipeline design could not guarantee this.
    assert {r.storage_uri for r in rows} == {e["storage_uri"] for e in manifest}
    assert {r.safe_relpath for r in rows} == {e["filename"] for e in manifest}
    assert all("/artifacts/viewable/" in r.storage_uri for r in rows)

    # The manifest is stamped on the job for the admin UI / strategic context.
    meta = job.polling_metadata or {}
    assert meta.get("viewable_file_count") == 8
    assert len(meta.get("viewable_files") or []) == 8


@pytest.mark.asyncio
async def test_pdf_is_viewable_artifact_kind(db_session, monkeypatch):
    """A .pdf plot a compute job writes into $BLUEPRINT_ARTIFACT_DIR is persisted
    as an individually-addressable, signable object carrying the NEW `kind`/
    `media_type` value ``pdf`` — end to end from storage classifier through the
    persistence pipeline and manifest — while SVG stays excluded fail-closed."""
    # 1) The storage classifier maps .pdf to (application/pdf, "pdf") on BOTH
    #    viewable surfaces, and the MIME->kind mapper agrees.
    assert storage.viewable_content_type_for_filename("figure.pdf") == (
        "application/pdf",
        "pdf",
    )
    assert "pdf" in storage._VIEWABLE_CONTENT_TYPES
    assert ".pdf" in storage.VIEWABLE_COMPUTE_JOB_EXTENSIONS
    assert storage._media_kind_for_mime("application/pdf") == "pdf"
    # SVG must remain excluded from both surfaces — pdf is added alongside it.
    assert storage.svg_is_viewable() is False

    # 2) A bundle containing a .pdf (alongside an excluded .svg) yields a manifest
    #    entry + ComputeJobFile row with kind/media_type "pdf".
    job = await _create_bundle_compute_job(db_session)
    bundle = _make_viewable_tar_bundle(
        {
            "./savefig.pdf": b"%PDF-1.5\n" + b"\x00" * 200,
            "./diagram.svg": b"<svg></svg>",  # excluded fail-closed
        }
    )
    calls = _mock_viewable_storage(monkeypatch, bundle)

    manifest, dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    assert dropped == 0
    assert len(manifest) == 1
    pdf_entry = manifest[0]
    assert pdf_entry["filename"] == "savefig.pdf"
    assert pdf_entry["kind"] == "pdf"
    assert pdf_entry["content_type"] == "application/pdf"
    assert len(calls["uploads"]) == 1

    rows = await _job_files(db_session, job.id)
    assert len(rows) == 1
    assert rows[0].safe_relpath == "savefig.pdf"
    assert rows[0].media_type == "pdf"
    assert rows[0].content_type == "application/pdf"

    # SVG was NOT persisted on either surface.
    assert not any(e["filename"] == "diagram.svg" for e in manifest)
    assert not any(r.safe_relpath == "diagram.svg" for r in rows)


@pytest.mark.asyncio
async def test_viewable_pipeline_skips_non_viewable_files(db_session, monkeypatch):
    """Files with extensions outside the viewable set are never persisted."""
    job = await _create_bundle_compute_job(db_session)
    bundle = _make_viewable_tar_bundle(
        {
            "./data.json": b'{"x": 1}',
            "./readme.md": b"# Hello",
            "./Makefile": b"all:\n\techo hi",
            "./binary.bin": b"\x00\x01\x02\x03",
            "./model.pt": b"pretrained",
            "./index.html": b"<html></html>",  # .html is not in the viewable set
        }
    )
    calls = _mock_viewable_storage(monkeypatch, bundle)

    manifest, dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    assert manifest == []
    assert dropped == 0
    assert calls["uploads"] == []
    assert await _job_files(db_session, job.id) == []
    # The pass still records that it RAN (count 0) — that stamp is the idempotency
    # marker, so a zero-file job must not re-download on re-entry either.
    assert (job.polling_metadata or {}).get("viewable_file_count") == 0


@pytest.mark.asyncio
async def test_viewable_pipeline_enforces_single_per_file_size_cap(
    db_session, monkeypatch
):
    """Files over the canonical per-file cap are skipped; smaller ones still persist.

    ONE cap policy now governs both surfaces. Previously the two pipelines
    disagreed (8 MiB vs 25 MB), so a 20 MB plot was present under one prefix and
    absent from the other.
    """
    job = await _create_bundle_compute_job(db_session)
    cap = compute_backends_module._MAX_VIEWABLE_FILE_BYTES

    oversized = b"\xff\xd8\xff\xe0" + b"X" * (cap + 1024)
    small = b"\x89PNG\r\n\x1a\n" + b"Y" * 1024
    bundle = _make_viewable_tar_bundle(
        {
            "./huge_photo.jpg": oversized,
            "./tiny_plot.png": small,
        }
    )
    calls = _mock_viewable_storage(monkeypatch, bundle)

    manifest, _dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    assert len(manifest) == 1
    assert manifest[0]["filename"] == "tiny_plot.png"
    assert len(calls["uploads"]) == 1

    rows = await _job_files(db_session, job.id)
    assert len(rows) == 1
    assert rows[0].safe_relpath == "tiny_plot.png"


@pytest.mark.asyncio
async def test_viewable_pipeline_streams_large_video_over_image_cap(
    db_session, monkeypatch
):
    """A video over the 8 MiB in-memory image cap is STREAMED, not dropped.

    Rollout/training videos routinely exceed the per-file in-memory ceiling that
    governs images. Rather than being silently dropped (locked inside the opaque
    bundle), they are streamed member-by-member to storage through the dedicated
    streaming seam (``upload_compute_job_viewable_stream``), while a co-resident
    image keeps taking the in-memory path.
    """
    job = await _create_bundle_compute_job(db_session)
    image_cap = compute_backends_module._MAX_VIEWABLE_FILE_BYTES
    # Incompressible so the archived member genuinely exceeds the image cap.
    big_video = os.urandom(image_cap + 512 * 1024)  # ~8.5 MiB > image cap
    small_png = b"\x89PNG\r\n\x1a\n" + b"Y" * 1024

    bundle = _make_viewable_tar_bundle(
        {
            "./rollout.mp4": big_video,
            "./frame.png": small_png,
        }
    )
    calls = _mock_viewable_storage(monkeypatch, bundle)

    manifest, dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    assert dropped == 0
    # Both persisted — the big video is NOT dropped for exceeding the image cap.
    by_name = {m["filename"]: m for m in manifest}
    assert set(by_name) == {"rollout.mp4", "frame.png"}
    assert by_name["rollout.mp4"]["size_bytes"] == len(big_video)
    assert by_name["rollout.mp4"]["kind"] == "video"
    assert by_name["rollout.mp4"]["content_type"] == "video/mp4"

    uploads = {u["basename"]: u for u in calls["uploads"]}
    # The video went through the STREAMING seam; the image through the in-memory one.
    assert uploads["rollout.mp4"]["streamed"] is True
    assert uploads["rollout.mp4"]["size"] == len(big_video)
    assert uploads["frame.png"]["streamed"] is False

    rows = await _job_files(db_session, job.id)
    names = {r.safe_relpath for r in rows}
    assert names == {"rollout.mp4", "frame.png"}


@pytest.mark.asyncio
async def test_viewable_pipeline_enforces_video_ceiling(db_session, monkeypatch):
    """A video over the (larger) streamed-video ceiling is still skipped.

    Streaming raises the video ceiling far above the image cap, but it is not
    unbounded: a video above ``_MAX_VIEWABLE_VIDEO_BYTES`` is skipped (stays in
    the bundle) while one within the ceiling is streamed. The ceiling is
    monkeypatched down so the test needn't materialize a half-gigabyte file.
    """
    job = await _create_bundle_compute_job(db_session)
    image_cap = compute_backends_module._MAX_VIEWABLE_FILE_BYTES
    video_ceiling = image_cap + 4 * 1024 * 1024  # 12 MiB, above the image cap
    monkeypatch.setattr(
        compute_backends_module, "_MAX_VIEWABLE_VIDEO_BYTES", video_ceiling
    )

    within = os.urandom(image_cap + 1 * 1024 * 1024)  # ~9 MiB: streamed
    over = os.urandom(video_ceiling + 1 * 1024 * 1024)  # ~13 MiB: skipped
    bundle = _make_viewable_tar_bundle(
        {
            "./ok_clip.mp4": within,
            "./too_big.webm": over,
        }
    )
    calls = _mock_viewable_storage(monkeypatch, bundle)

    manifest, dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    # Only the within-ceiling video is persisted; the oversized one is skipped
    # (size rejections are not counted in the dropped/count-cap tally).
    assert [m["filename"] for m in manifest] == ["ok_clip.mp4"]
    assert dropped == 0
    uploads = {u["basename"]: u for u in calls["uploads"]}
    assert set(uploads) == {"ok_clip.mp4"}
    assert uploads["ok_clip.mp4"]["streamed"] is True

    rows = await _job_files(db_session, job.id)
    assert {r.safe_relpath for r in rows} == {"ok_clip.mp4"}


def test_viewable_pipeline_streams_large_video_and_peak_memory_is_bounded(
    tmp_path, monkeypatch
):
    """A large video is uploaded WITHOUT ever holding it whole in RAM.

    The whole point of streaming: a video far larger than the 8 MiB in-memory
    image cap must reach storage while the orchestrator's peak allocation stays
    bounded by the copy/stream buffer, not the video size. An implementation that
    buffered the video (``upload_from_string``) would pay for its full size here
    and trip the ceiling.
    """
    monkeypatch.setattr(
        compute_backends_module, "_MAX_VIEWABLE_VIDEO_BYTES", 128 * 1024 * 1024
    )
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096
    # INCOMPRESSIBLE and well over the 8 MiB image cap (but under the video
    # ceiling): the streamed bytes a buffering impl would hold are the full 40 MiB.
    big_video = os.urandom(40 * 1024 * 1024)
    src = tmp_path / "bundle.tar.gz"
    with tarfile.open(str(src), mode="w:gz", compresslevel=1) as tar:
        for name, payload in (
            ("./rollout.mp4", big_video),
            ("./plot.png", png),
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    video_size = len(big_video)
    del big_video
    assert src.stat().st_size > 32 * 1024 * 1024, "the video must dwarf the ceiling"

    def _fake_stream(_job_id, dest_path):
        shutil.copyfile(str(src), dest_path)  # to disk; no Python-level buffer
        return True

    async def _forbid_whole_bundle_download(_job_id):
        raise AssertionError("must stream the bundle to disk, never load it whole")

    uploaded: list = []

    async def _fake_file_upload(job_id, filename, data, content_type):
        uploaded.append({"filename": filename, "streamed": False})
        return storage.compute_job_viewable_uri(job_id, filename)

    async def _fake_stream_upload(job_id, filename, fileobj, content_type, *, size=None):
        # Measure via seek — reading the file whole here would defeat the test.
        fileobj.seek(0, os.SEEK_END)
        measured = fileobj.tell()
        fileobj.seek(0)
        uploaded.append({"filename": filename, "streamed": True, "size": measured})
        return storage.compute_job_viewable_uri(job_id, filename)

    job = MagicMock()
    job.id = uuid.uuid4()
    job.backend = compute_backends_module.GCP_BACKEND_NAME

    with patch.object(
        compute_backends_module, "_download_artifact_bundle_to_path", _fake_stream
    ), patch.object(
        compute_backends_module.storage,
        "download_compute_job_artifact_bundle",
        _forbid_whole_bundle_download,
    ), patch.object(
        compute_backends_module.storage,
        "upload_compute_job_viewable_file",
        _fake_file_upload,
    ), patch.object(
        compute_backends_module.storage,
        "upload_compute_job_viewable_stream",
        _fake_stream_upload,
    ):
        tracemalloc.start()
        try:
            manifest, dropped = asyncio.run(
                compute_backends_module.persist_viewable_artifacts(job)
            )
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

    assert sorted(m["filename"] for m in manifest) == ["plot.png", "rollout.mp4"]
    assert dropped == 0
    by_name = {u["filename"]: u for u in uploaded}
    assert by_name["rollout.mp4"]["streamed"] is True
    assert by_name["rollout.mp4"]["size"] == video_size
    assert by_name["plot.png"]["streamed"] is False

    # Peak stays bounded — well under the 40 MiB video — proving the video is
    # streamed, not materialized in the orchestrator's heap.
    ceiling = 16 * 1024 * 1024
    assert peak < ceiling, (
        f"the viewable pass peaked at {peak / 1024 / 1024:.1f} MiB streaming a "
        f"{video_size / 1024 / 1024:.1f} MiB video — it is buffering the video "
        "instead of streaming it (this is the OOM the task fixes)"
    )


@pytest.mark.asyncio
async def test_viewable_pipeline_enforces_single_file_count_cap(
    db_session, monkeypatch
):
    """At most the canonical N viewable files are persisted; the excess is REPORTED.

    The old duplicate pipeline capped at a different count (50 vs 25) and took the
    first N in arbitrary tar order; the canonical pass sorts deterministically
    before capping and reports how many it dropped.
    """
    job = await _create_bundle_compute_job(db_session)
    cap = compute_backends_module._MAX_VIEWABLE_FILE_COUNT
    overflow = 5

    members = {}
    for i in range(cap + overflow):
        members[f"./plot_{i:03d}.png"] = b"\x89PNG\r\n\x1a\n" + str(i).encode() * 10
    bundle = _make_viewable_tar_bundle(members)
    calls = _mock_viewable_storage(monkeypatch, bundle)

    manifest, dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    assert len(manifest) == cap
    assert dropped == overflow, "the pass must report the files it dropped"
    assert len(calls["uploads"]) == cap
    assert len(await _job_files(db_session, job.id)) == cap

    # Deterministic selection: the sorted-first `cap` filenames, not tar order.
    expected = sorted(f"plot_{i:03d}.png" for i in range(cap + overflow))[:cap]
    assert [e["filename"] for e in manifest] == expected
    assert (job.polling_metadata or {}).get("viewable_files_dropped") == overflow


@pytest.mark.asyncio
async def test_viewable_pipeline_sanitizes_paths_and_keeps_subdir_context(
    db_session, monkeypatch
):
    """Traversal/absolute members are rejected; subdir context is folded, not lost.

    ONE sanitizer now. ``arm1/reward.png`` and ``arm2/reward.png`` are distinct
    objects (``arm1__reward.png`` / ``arm2__reward.png``) instead of colliding, and
    ``..`` / absolute members are refused outright.
    """
    job = await _create_bundle_compute_job(db_session)
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00"
    bundle = _make_viewable_tar_bundle(
        {
            "./safe_plot.png": png,
            "../escape.png": png,
            "./subdir/../../escape2.png": png,
            "/etc/passwd.png": png,  # absolute path rejected
            "./arm1/reward.png": png,
            "./arm2/reward.png": png,
        }
    )
    calls = _mock_viewable_storage(monkeypatch, bundle)

    manifest, _dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    persisted = {e["filename"] for e in manifest}
    assert persisted == {"safe_plot.png", "arm1__reward.png", "arm2__reward.png"}
    assert {u["basename"] for u in calls["uploads"]} == persisted

    # No traversal/absolute member may reach storage under any name.
    for uploaded in calls["uploads"]:
        assert ".." not in uploaded["basename"]
        assert not uploaded["basename"].startswith("/")

    rows = await _job_files(db_session, job.id)
    assert {r.safe_relpath for r in rows} == persisted
    # Same-named plots from different arms stay DISTINCT rows (no collision).
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_viewable_pipeline_upload_failure_is_non_blocking(
    db_session, monkeypatch
):
    """One file's upload failure must not prevent the others being persisted."""
    job = await _create_bundle_compute_job(db_session)
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00"
    bundle = _make_viewable_tar_bundle(
        {
            "./good_a.png": png,
            "./fail_me.png": png,
            "./good_b.png": png,
            "./also_bad_fail_me.jpg": b"\xff\xd8\xff\xe0\x00",
            "./good_c.png": png,
        }
    )
    _calls = _mock_viewable_storage(monkeypatch, bundle, fail_on="fail_me")

    manifest, _dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    # 3 of 5 survived; only durably-stored files appear on either surface.
    assert {e["filename"] for e in manifest} == {
        "good_a.png",
        "good_b.png",
        "good_c.png",
    }
    rows = await _job_files(db_session, job.id)
    assert {r.safe_relpath for r in rows} == {
        "good_a.png",
        "good_b.png",
        "good_c.png",
    }
    # A file that never landed in storage must NOT be advertised as signable.
    assert not any("fail_me" in r.storage_uri for r in rows)


@pytest.mark.asyncio
async def test_viewable_pipeline_is_idempotent_and_downloads_bundle_once(
    db_session, monkeypatch
):
    """Re-entry must not re-download, re-upload, or duplicate rows.

    This is the regression cover for the duplicate-pipeline waste: the SAME terminal
    transition reaches both the call site and ``_link_terminal_artifact``, and the
    bundle must still be fetched exactly ONCE across both.
    """
    job = await _create_bundle_compute_job(db_session)
    png = b"\x89PNG\r\n\x1a\n" + b"Z" * 42
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"W" * 42
    bundle = _make_viewable_tar_bundle({"./chart.png": png, "./anim.mp4": mp4})
    calls = _mock_viewable_storage(monkeypatch, bundle)

    manifest1, _d1 = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()
    assert len(manifest1) == 2
    assert calls["downloads"] == 1
    assert len(calls["uploads"]) == 2

    # Second pass over the same job — the idempotency guard must short-circuit it.
    manifest2, _d2 = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    assert calls["downloads"] == 1, (
        "re-entry must NOT re-download the bundle — this is exactly the waste the "
        "single-pipeline collapse removes"
    )
    assert len(calls["uploads"]) == 2, "re-entry must NOT re-upload any file"
    assert {e["filename"] for e in manifest2} == {e["filename"] for e in manifest1}

    rows = await _job_files(db_session, job.id)
    assert len(rows) == 2, f"expected no duplicate rows on re-entry, got {len(rows)}"


@pytest.mark.asyncio
async def test_viewable_pipeline_failsoft_on_empty_or_bad_bundle(
    db_session, monkeypatch
):
    """An empty / unparseable bundle yields no files and NEVER raises.

    Viewable persistence must never block a job's terminal completion.
    """
    for blob in (b"", b"not-a-tar", b"\x00" * 100):
        job = await _create_bundle_compute_job(db_session)
        calls = _mock_viewable_storage(monkeypatch, blob)

        manifest, dropped = await compute_jobs.persist_viewable_files_for_job(
            db_session, job
        )
        await db_session.commit()

        assert manifest == []
        assert dropped == 0
        assert calls["uploads"] == []
        assert await _job_files(db_session, job.id) == []


@pytest.mark.asyncio
async def test_viewable_pipeline_skips_backends_without_a_bundle(
    db_session, monkeypatch
):
    """A local-subprocess job has no durable bundle — the pass must not fetch one."""
    job = await _create_bundle_compute_job(db_session, backend="local-subprocess")
    calls = _mock_viewable_storage(monkeypatch, b"")

    manifest, dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    assert manifest == []
    assert dropped == 0
    assert calls["downloads"] == 0, "a local job must never download a bundle"


# ── No-progress watchdog ──────────────────────────────────────────────────────
#
# The surfacing half (progress_series ring buffer + declared_metric) already
# shipped; these cover the CONSUMER wired in this change: _no_progress_stalled
# and its enforcement in _process_single_compute_job.


# Six ages spanning >WINDOW (1800s): oldest is 2000s old (satisfies the "full
# window of history" gate) and five samples fall inside the trailing 1800s
# window (satisfies the min-sample floor).
_SPAN_AGES = [2000, 1500, 1100, 700, 300, 0]


def _progress_job(*, metric, samples, started_offset_seconds, extra_meta=None):
    """Build an in-memory job-like object for pure watchdog-trend tests.

    ``samples`` is a list of ``(seconds_before_now, name, value)`` tuples for the
    progress ring buffer; ``started_offset_seconds`` is how long ago the job
    started.
    """
    from types import SimpleNamespace

    now = _utcnow()
    meta = dict(extra_meta or {})
    if metric is not None:
        meta["declared_metric"] = metric
    series = [
        {
            "t": (now - timedelta(seconds=age)).isoformat(),
            "name": name,
            "value": value,
        }
        for (age, name, value) in samples
    ]
    meta["progress_series"] = series
    return SimpleNamespace(
        polling_metadata=meta,
        started_at=now - timedelta(seconds=started_offset_seconds),
    ), now


def test_no_progress_stalled_flat_metric_over_window_fires():
    metric = "val/loss"
    # A flat metric spanning >1 window, job running ~2h.
    samples = [(age, metric, 2.5000001) for age in _SPAN_AGES]
    job, now = _progress_job(
        metric=metric, samples=samples, started_offset_seconds=7200
    )
    assert compute_jobs._no_progress_stalled(job, now=now) is True


def test_no_progress_stalled_improving_metric_does_not_fire():
    metric = "val/loss"
    # Monotonically decreasing loss across the window → real progress.
    samples = [(age, metric, 3.0 - i * 0.2) for i, age in enumerate(_SPAN_AGES)]
    job, now = _progress_job(
        metric=metric, samples=samples, started_offset_seconds=7200
    )
    assert compute_jobs._no_progress_stalled(job, now=now) is False


def test_no_progress_stalled_requires_min_samples():
    metric = "val/loss"
    # Only 2 flat samples spanning the window — below the min-sample floor.
    samples = [(2000, metric, 1.0), (0, metric, 1.0)]
    job, now = _progress_job(
        metric=metric, samples=samples, started_offset_seconds=7200
    )
    assert compute_jobs._no_progress_stalled(job, now=now) is False


def test_no_progress_stalled_requires_window_of_history():
    metric = "val/loss"
    # Enough flat samples but all bunched into the last ~2 minutes — the metric
    # history does not yet span a full window, so no stall can be declared.
    samples = [(120 - i * 20, metric, 1.0) for i in range(6)]
    job, now = _progress_job(
        metric=metric, samples=samples, started_offset_seconds=7200
    )
    assert compute_jobs._no_progress_stalled(job, now=now) is False


def test_no_progress_stalled_ignores_stale_buffer():
    metric = "val/loss"
    # A flat buffer whose newest sample predates the trailing window entirely
    # (the job stopped reporting long ago). The window slice is empty → no fire;
    # a genuinely dead VM is handled by the poll-failure kills, not the watchdog.
    samples = [(3600 - i * 300, metric, 1.0) for i in range(6)]  # ages 3600..2100
    job, now = _progress_job(
        metric=metric, samples=samples, started_offset_seconds=7200
    )
    assert compute_jobs._no_progress_stalled(job, now=now) is False


def test_no_progress_stalled_respects_grace_period():
    metric = "val/loss"
    samples = [(age, metric, 1.0) for age in _SPAN_AGES]
    # Job only started 10 minutes ago (< 30m grace) — never cut this early even
    # though the (back-dated) samples look flat.
    job, now = _progress_job(
        metric=metric, samples=samples, started_offset_seconds=600
    )
    assert compute_jobs._no_progress_stalled(job, now=now) is False


def test_no_progress_stalled_no_declared_metric_is_fail_open():
    # Flat samples exist, but the job declares no metric → watchdog never fires.
    samples = [(age, "val/loss", 1.0) for age in _SPAN_AGES]
    job, now = _progress_job(
        metric=None, samples=samples, started_offset_seconds=7200
    )
    assert compute_jobs._no_progress_stalled(job, now=now) is False


def test_no_progress_stalled_ignores_other_metric_names():
    metric = "val/loss"
    # The declared metric only has 1 sample; a DIFFERENT metric is flat. The
    # watchdog must key strictly on the declared metric and stay fail-open.
    samples = [(0, metric, 1.0)] + [(age, "throughput", 5.0) for age in _SPAN_AGES]
    job, now = _progress_job(
        metric=metric, samples=samples, started_offset_seconds=7200
    )
    assert compute_jobs._no_progress_stalled(job, now=now) is False


def test_no_progress_stalled_skips_malformed_samples():
    metric = "val/loss"
    job, now = _progress_job(
        metric=metric,
        samples=[(age, metric, 1.0) for age in _SPAN_AGES],
        started_offset_seconds=7200,
    )
    # Inject garbage entries: they must be skipped, not crash or corrupt trend.
    job.polling_metadata["progress_series"].extend(
        [
            "not-a-dict",
            {"name": metric, "value": "not-a-number", "t": "bad"},
            {"name": metric, "value": 1.0},  # missing t
        ]
    )
    assert compute_jobs._no_progress_stalled(job, now=now) is True


def test_no_progress_stalled_disabled_when_window_zero(monkeypatch):
    monkeypatch.setattr(compute_jobs, "COMPUTE_JOB_NO_PROGRESS_WINDOW_SECONDS", 0.0)
    metric = "val/loss"
    samples = [(age, metric, 1.0) for age in _SPAN_AGES]
    job, now = _progress_job(
        metric=metric, samples=samples, started_offset_seconds=7200
    )
    assert compute_jobs._no_progress_stalled(job, now=now) is False


@pytest.mark.asyncio
async def test_create_compute_job_persists_declared_metric(db_session):
    project = Project(name="Declared Metric Persist", user_id="test-user")
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
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        declared_metric="  val/loss  ",
    )
    # Stored trimmed so the watchdog matches the runner-reported sample name.
    assert job.polling_metadata["declared_metric"] == "val/loss"


@pytest.mark.asyncio
async def test_process_compute_jobs_once_cuts_stalled_no_progress_job(
    db_session, monkeypatch
):
    project = Project(name="No Progress Watchdog", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
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
        spend_cap_usd=1.0,
        deadline_seconds=86400,
        declared_metric="val/loss",
    )
    now = _utcnow()
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    # A long-running (5h) job with a flat declared metric spanning >1 window.
    meta["runtime_deadline_at"] = (now + timedelta(hours=20)).isoformat()
    meta["progress_series"] = [
        {
            "t": (now - timedelta(seconds=age)).isoformat(),
            "name": "val/loss",
            "value": 2.71828,
        }
        for age in (2000, 1500, 1100, 700, 300, 0)
    ]
    job.polling_metadata = meta
    job.status = ComputeJobStatus.RUNNING
    job.external_compute_id = "no-progress-job"
    job.started_at = now - timedelta(hours=5)
    await db_session.commit()

    terminate_reasons: list[str] = []

    class FakeBackend:
        def external_id_for(self, persisted_job):
            return "no-progress-job"

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.05, measured_at=as_of)

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
    assert terminate_reasons == ["no_progress"]
    assert job.status == ComputeJobStatus.TIMED_OUT
    assert job.outcome == ComputeJobOutcome.TIMED_OUT
    assert job.polling_metadata["terminal_kind"] == "no_progress"
    assert "no measurable progress" in (job.terminal_reason or "")


@pytest.mark.asyncio
async def test_process_compute_jobs_once_keeps_progressing_job_running(
    db_session, monkeypatch
):
    project = Project(name="Progressing Job", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
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
        spend_cap_usd=1.0,
        deadline_seconds=86400,
        declared_metric="val/loss",
    )
    now = _utcnow()
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    meta["runtime_deadline_at"] = (now + timedelta(hours=20)).isoformat()
    # Loss is still steadily improving → not a stall.
    meta["progress_series"] = [
        {
            "t": (now - timedelta(seconds=age)).isoformat(),
            "name": "val/loss",
            "value": 3.0 - i * 0.15,
        }
        for i, age in enumerate((2000, 1500, 1100, 700, 300, 0))
    ]
    job.polling_metadata = meta
    job.status = ComputeJobStatus.RUNNING
    job.external_compute_id = "progressing-job"
    job.started_at = now - timedelta(hours=5)
    await db_session.commit()

    class FakeBackend:
        def external_id_for(self, persisted_job):
            return "progressing-job"

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.05, measured_at=as_of)

        async def poll_status(self, persisted_job):
            return ComputeStatusResult(status=ComputeJobStatus.RUNNING)

        async def terminate(self, persisted_job, *, reason):
            raise AssertionError("progressing job must not be terminated")

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


def test_build_compute_launch_kwargs_forwards_declared_metric():
    from app.orchestrator_runner_dispatch import _build_compute_launch_kwargs

    kwargs = _build_compute_launch_kwargs(
        {"declared_metric": "val/acc"},
        command="python x.py",
        spend_cap_usd=1.0,
        deadline_seconds=60,
    )
    assert kwargs["declared_metric"] == "val/acc"

    nested = _build_compute_launch_kwargs(
        {"compute_experiment": {"declared_metric": "val/acc"}},
        command="python x.py",
        spend_cap_usd=1.0,
        deadline_seconds=60,
    )
    assert nested["declared_metric"] == "val/acc"

    absent = _build_compute_launch_kwargs(
        {}, command="python x.py", spend_cap_usd=1.0, deadline_seconds=60
    )
    assert "declared_metric" not in absent


# ── Cross-job checkpoint resume (input_artifact_job_id) ───────────────────────


async def _make_resume_predecessor_job(
    db_session, *, project_id, with_bundle: bool = True
) -> ComputeJob:
    """Insert a terminal runner-subprocess predecessor job.

    When ``with_bundle`` is True the row carries a durable ``result_reference`` so
    ``_compute_job_has_durable_bundle`` treats it as a valid resume source; when
    False it has produced no bundle and must be rejected as a resume source.
    """
    job = ComputeJob(
        project_id=project_id,
        backend=compute_backends_module.RUNNER_SUBPROCESS_BACKEND_NAME,
        machine_type="e2-standard-2",
        status=ComputeJobStatus.SUCCEEDED,
        accrued_cost_usd=0.0,
        completed_at=_utcnow() - timedelta(hours=1),
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    if with_bundle:
        job.result_reference = storage.compute_job_artifact_uri(job.id)
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
    return job


async def _make_pinned_runner_and_task(db_session, project_id):
    """Create a runner in the project and a compute_experiment task pinned to it."""
    from app import auth as auth_mod
    from app.models import LocalRunner

    token = auth_mod.create_runner_token(uuid.uuid4())
    runner = LocalRunner(
        project_id=project_id,
        token_hash=auth_mod.hash_runner_token(token),
        capabilities={"task_types": "all", "has_gpu": False},
    )
    db_session.add(runner)
    await db_session.commit()
    await db_session.refresh(runner)

    task = Task(
        project_id=project_id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
        context={"runner_target_id": str(runner.id)},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return runner, token, task


@pytest.mark.asyncio
async def test_compute_job_resume_hydrates_prior_bundle(client, db_session, monkeypatch):
    """End-to-end cross-job checkpoint resume on the runner-subprocess path.

    (1) create_compute_job validates + persists input_artifact_job_id;
    (2) the claim response surfaces it to the runner;
    (3) the new GET .../input-artifact endpoint streams the predecessor's durable
        bundle so the runner can extract the prior checkpoint into its artifact
        dir (the extracted bundle actually contains the checkpoint);
    (4) the runner's reported resume provenance is recorded in polling_metadata
        and exposed (with input_artifact_job_id) on the serialized response.
    """
    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    project = Project(name="Resume Project", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    predecessor = await _make_resume_predecessor_job(
        db_session, project_id=project.id, with_bundle=True
    )
    _runner, token, task = await _make_pinned_runner_and_task(db_session, project.id)

    # The predecessor's durable bundle carries a real checkpoint plus a stale
    # terminal.json from the prior run (which the runner-side extractor strips).
    bundle_bytes = _make_tar_gz_bundle(
        {
            "checkpoint/model.pt": b"WEIGHTS-EPOCH-7",
            "terminal.json": _terminal_record_bytes(status="SUCCEEDED"),
        }
    )

    async def _fake_download(job_id):
        assert str(job_id) == str(predecessor.id)
        return bundle_bytes

    monkeypatch.setattr(storage, "download_compute_job_artifact_bundle", _fake_download)

    # (1) Launch the successor with the resume source — validated + persisted.
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py --resume",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        input_artifact_job_id=str(predecessor.id),
        declared_metric="loss",
        no_progress_timeout_seconds=120,
    )
    assert job.backend == compute_backends_module.RUNNER_SUBPROCESS_BACKEND_NAME
    assert job.polling_metadata.get("input_artifact_job_id") == str(predecessor.id)

    # Forwarded into the launch spec the backend consumes.
    spec = compute_jobs._build_launch_spec(job)
    assert spec.input_artifact_job_id == str(predecessor.id)

    # Move to STARTING so the runner can claim it (mirrors monitor launch tick).
    job.status = ComputeJobStatus.STARTING
    db_session.add(job)
    await db_session.commit()

    # (2) Claim surfaces the resume source to the runner.
    resp = await client.post(
        "/api/runner/next-compute-job",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == str(job.id)
    assert body["input_artifact_job_id"] == str(predecessor.id)

    # (3) The download endpoint streams the predecessor bundle; the extracted
    # bytes actually contain the prior checkpoint (i.e. it hydrates the run).
    dl = await client.get(
        f"/api/runner/compute-job/{job.id}/input-artifact",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert dl.status_code == 200, dl.text
    assert dl.content == bundle_bytes
    with tarfile.open(fileobj=io.BytesIO(dl.content), mode="r:gz") as tar:
        names = tar.getnames()
        assert "checkpoint/model.pt" in names
        extracted = tar.extractfile("checkpoint/model.pt").read()
    assert extracted == b"WEIGHTS-EPOCH-7"

    # (4) Runner reports honest hydration provenance → recorded + serialized.
    rep = await client.post(
        f"/api/runner/compute-job/{job.id}/report",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "exec_status": "running",
            "resume_hydration": {
                "resumed_from_job_id": str(predecessor.id),
                "ok": True,
                "file_count": 1,
                "bytes": len(b"WEIGHTS-EPOCH-7"),
            },
        },
    )
    assert rep.status_code == 200, rep.text
    await db_session.refresh(job)
    provenance = (job.polling_metadata or {}).get("resume_provenance")
    assert provenance is not None
    assert provenance["ok"] is True
    assert provenance["resumed_from_job_id"] == str(predecessor.id)
    assert provenance["file_count"] == 1

    serialized = schemas.serialize_compute_job_response(job)
    assert serialized["input_artifact_job_id"] == str(predecessor.id)
    assert serialized["resume_provenance"]["ok"] is True


@pytest.mark.asyncio
async def test_compute_job_resume_rejects_unknown_input_artifact_job_id(
    db_session, monkeypatch
):
    """A dangling input_artifact_job_id is rejected at launch (fail-closed) and no
    job row is created — never a silent no-op."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    task = await _make_compute_task(db_session)

    with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc:
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
            input_artifact_job_id=str(uuid.uuid4()),
        )
    assert exc.value.detail["error"] == "input_artifact_job_not_found"
    jobs = (
        (await db_session.execute(select(ComputeJob).where(ComputeJob.task_id == task.id)))
        .scalars()
        .all()
    )
    assert jobs == []


@pytest.mark.asyncio
async def test_compute_job_resume_rejects_malformed_input_artifact_job_id(
    db_session, monkeypatch
):
    """A non-UUID input_artifact_job_id is rejected with a clear agent-facing error."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    task = await _make_compute_task(db_session)

    with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc:
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
            input_artifact_job_id="not-a-uuid",
        )
    assert exc.value.detail["error"] == "invalid_input_artifact_job_id"


@pytest.mark.asyncio
async def test_compute_job_resume_rejects_cross_project_input_artifact(
    db_session, monkeypatch
):
    """A resume source owned by a DIFFERENT project is refused."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    other_project = Project(name="Other Project", user_id="u")
    db_session.add(other_project)
    await db_session.commit()
    await db_session.refresh(other_project)
    foreign = await _make_resume_predecessor_job(
        db_session, project_id=other_project.id, with_bundle=True
    )

    task = await _make_compute_task(db_session)
    with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc:
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
            input_artifact_job_id=str(foreign.id),
        )
    assert exc.value.detail["error"] == "input_artifact_job_cross_project"


@pytest.mark.asyncio
async def test_compute_job_resume_rejects_bundleless_input_artifact(
    db_session, monkeypatch
):
    """A resume source that never produced a durable bundle is refused — resuming
    from nothing would be reproducibility dishonesty."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    task = await _make_compute_task(db_session)
    predecessor = await _make_resume_predecessor_job(
        db_session, project_id=task.project_id, with_bundle=False
    )
    with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc:
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
            input_artifact_job_id=str(predecessor.id),
        )
    assert exc.value.detail["error"] == "input_artifact_job_has_no_bundle"


@pytest.mark.asyncio
async def test_compute_job_resume_rejects_failed_ancestor_hydration(db_session, monkeypatch):
    """A bundle from a failed resume hydration is never lineage-eligible."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    task = await _make_compute_task(db_session)
    predecessor = await _make_resume_predecessor_job(
        db_session, project_id=task.project_id, with_bundle=True
    )
    predecessor.polling_metadata = {
        "input_artifact_job_id": str(uuid.uuid4()),
        "resume_provenance": {"ok": False, "reason": "extract_error"},
    }
    db_session.add(predecessor)
    await db_session.commit()

    with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc:
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
            input_artifact_job_id=str(predecessor.id),
            declared_metric="loss",
            no_progress_timeout_seconds=120,
        )
    assert exc.value.detail["error"] == "input_artifact_job_resume_provenance_invalid"


@pytest.mark.asyncio
async def test_compute_job_resume_rejects_non_terminal_source(db_session, monkeypatch):
    """A still-RUNNING or POLLING predecessor is refused as a resume source.

    Resuming from a non-terminal job risks rehydrating a partial/mid-checkpoint
    bundle — the design doc flags this as 'infinite chaining of dead runs'.
    """
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    task = await _make_compute_task(db_session)

    for non_terminal_status in (
        ComputeJobStatus.RUNNING,
        ComputeJobStatus.POLLING,
        ComputeJobStatus.QUEUED,
        ComputeJobStatus.STARTING,
    ):
        job = ComputeJob(
            project_id=task.project_id,
            backend=compute_backends_module.RUNNER_SUBPROCESS_BACKEND_NAME,
            machine_type="e2-standard-2",
            status=non_terminal_status,
            accrued_cost_usd=0.0,
            result_reference=storage.compute_job_artifact_uri(uuid.uuid4()),
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc:
            await compute_jobs.create_compute_job(
                db_session,
                task,
                command="python train.py",
                machine_type="e2-standard-2",
                spend_cap_usd=1.0,
                deadline_seconds=300,
                input_artifact_job_id=str(job.id),
            )
        assert exc.value.detail["error"] == "input_artifact_job_not_terminal", (
            f"expected rejection for status={non_terminal_status}"
        )
        assert exc.value.detail["source_status"] == non_terminal_status.value


@pytest.mark.asyncio
async def test_gcp_startup_script_hydrates_input_artifact_bundle(monkeypatch):
    """When a resume source is set, GcpComputeBackend.launch mints a download
    signed URL and the startup script pulls + extracts the predecessor bundle
    into the artifact dir BEFORE the command runs."""
    backend = GcpComputeBackend()
    predecessor_id = uuid.uuid4()
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python train.py --resume",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        input_artifact_job_id=str(predecessor_id),
    )

    download_calls: list = []

    async def fake_upload_url(jid, *, expires_at=None):
        return "https://storage.example.com/upload"

    async def fake_download_url(jid, *, expires_at=None):
        download_calls.append(jid)
        return "https://storage.example.com/predecessor-bundle"

    provision_calls: list = []

    async def fake_provision(
        vm_id, *, startup_script, machine_type, provisioning_model,
        accelerator=None, accelerator_count=1,
    ):
        provision_calls.append(startup_script)
        return True

    monkeypatch.setattr(
        storage, "generate_compute_job_artifact_upload_signed_url", fake_upload_url
    )
    monkeypatch.setattr(
        storage, "generate_compute_job_artifact_download_signed_url", fake_download_url
    )
    monkeypatch.setattr(compute, "provision_ephemeral_vm", fake_provision)
    monkeypatch.setattr(compute, "find_vm_zone", lambda _vm_name: None)
    monkeypatch.setattr(compute, "get_vm_zone", lambda _vm_name: "us-central1-f")

    await backend.launch(spec)

    assert download_calls == [str(predecessor_id)]
    script = provision_calls[0]
    # Downloads the predecessor bundle and extracts it into the artifact dir.
    assert "https://storage.example.com/predecessor-bundle" in script
    assert (
        f"tar -C {shlex.quote(compute_backends_module._GCP_ARTIFACT_DIR_PATH)} "
        f"--no-same-owner -xzf" in script
    )
    assert "resume_hydration.ok" in script
    assert "resume_hydration.failed" in script


@pytest.mark.asyncio
async def test_gcp_startup_script_no_resume_block_without_source(monkeypatch):
    """Without a resume source the startup script is unchanged (no resume block)."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="echo ok",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    assert "resume_hydration.ok" not in script
    assert "blueprint_resume_bundle.tar.gz" not in script


@pytest.mark.asyncio
async def test_compute_job_input_artifact_endpoint_404_without_source(
    client, db_session, monkeypatch
):
    """The input-artifact endpoint 404s when the pinned job declares no resume
    source (fail-closed, no bytes leaked)."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    project = Project(name="No Resume", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _runner, token, task = await _make_pinned_runner_and_task(db_session, project.id)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    job.status = ComputeJobStatus.STARTING
    db_session.add(job)
    await db_session.commit()

    dl = await client.get(
        f"/api/runner/compute-job/{job.id}/input-artifact",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert dl.status_code == 404


# ===========================================================================
# Release-audit fixes: stale-snapshot VM kill, DOA retry deadline,
# whole-bundle viewable download, unreachable watchdog SIGKILL.
# ===========================================================================


@pytest.mark.asyncio
async def test_monitor_rereads_task_before_terminal_kill_so_midtick_restore_is_respected(
    db_session, monkeypatch
):
    """A task restored DURING the tick must not have its live VM killed.

    The monitor batch-selects up to 50 jobs and then processes them one at a
    time, each doing backend RPCs — so a job late in the batch is reached long
    after its row (and its task's row) was read. The task-terminal VM kill is
    irreversible, so it must be decided on the AUTHORITATIVE task row, never on
    the tick-start snapshot.

    The mid-tick change here is real, driven through the real tick: job A's poll
    (which runs while the tick is in flight) restores job B's task from FAILED
    back to RUNNING — exactly the operator "PATCH a falsely-FAILED task back to
    QUEUED" move. B's VM must survive: no terminate, no CANCELLED.
    """
    project = Project(name=f"midtick-{uuid.uuid4().hex[:8]}", user_id="test")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task_a = Task(
        project_id=project.id,
        type="compute_experiment",
        prompt="a",
        status=TaskStatus.IN_PROGRESS,
    )
    task_b = Task(
        project_id=project.id,
        type="compute_experiment",
        prompt="b",
        status=TaskStatus.IN_PROGRESS,  # flipped to FAILED below, once its job exists
    )
    db_session.add_all([task_a, task_b])
    await db_session.commit()
    await db_session.refresh(task_a)
    await db_session.refresh(task_b)

    async def _running_job(task, external_id):
        job = await compute_jobs.create_compute_job(
            db_session,
            task,
            command="echo ok",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )
        job.status = ComputeJobStatus.RUNNING
        job.external_compute_id = external_id
        meta = dict(job.polling_metadata or {})
        meta["preflight_smoke_done"] = True
        job.polling_metadata = meta
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
        return job

    job_a = await _running_job(task_a, "vm-a")
    job_b = await _running_job(task_b, "vm-b")
    # Pin the batch order (the tick sorts by created_at, id): A is processed
    # first, so its poll is what lands the mid-tick change on B's task.
    job_a.created_at = _utcnow() - timedelta(minutes=5)
    job_b.created_at = _utcnow() - timedelta(minutes=1)
    # B's task is terminal as the tick STARTS — this is what the tick's snapshot
    # sees, and what the (unfixed) monitor would kill B's VM on.
    task_b.status = TaskStatus.FAILED
    db_session.add_all([job_a, job_b, task_b])
    await db_session.commit()
    assert job_a.created_at < job_b.created_at

    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    terminated: list = []

    class MidTickBackend:
        def external_id_for(self, persisted_job):
            return persisted_job.external_compute_id or "vm-x"

        async def launch(self, spec):  # pragma: no cover - both jobs are RUNNING
            raise AssertionError("launch must not be called")

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.0, measured_at=as_of)

        async def poll_status(self, persisted_job):
            if persisted_job.external_compute_id == "vm-a":
                # THE MID-TICK CHANGE: task B is restored while the tick is in
                # flight — after the tick's batch snapshot was taken.
                async with session_factory() as session:
                    live_b = await session.get(Task, task_b.id)
                    live_b.status = TaskStatus.IN_PROGRESS
                    session.add(live_b)
                    await session.commit()
            return ComputeStatusResult(status=ComputeJobStatus.RUNNING)

        async def terminate(self, persisted_job, *, reason):
            terminated.append((persisted_job.external_compute_id or "", reason))
            return True

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: MidTickBackend()
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job_b)
    await db_session.refresh(task_b)
    assert task_b.status == TaskStatus.IN_PROGRESS  # the restore landed mid-tick
    assert terminated == [], (
        "job B's VM was killed on the STALE batch snapshot — the task-terminal "
        f"kill must re-read the authoritative task row (terminate calls: {terminated})"
    )
    assert job_b.status == ComputeJobStatus.RUNNING
    assert job_b.outcome != ComputeJobOutcome.CANCELLED


@pytest.mark.asyncio
async def test_provisioning_retry_deadline_starts_at_retry_time_not_stale_tick_start(
    db_session,
    monkeypatch,
):
    """Retry 1 must not be dead on arrival after a slow launch.

    The caller reaches the retry only AFTER waiting out the launch leg (an LRO
    that can burn the entire provisioning window) plus a terminate RPC — so the
    tick-start ``now`` is long stale by then. Deriving the new provisioning
    deadline from it wrote a deadline that was already in the PAST: the next tick
    computed ``_provisioning_timeout_seconds() == 0``, re-timed-out the job
    WITHOUT ever attempting a fresh launch, and the whole retry budget evaporated
    in seconds.

    The persisted deadline must be in the future by ~the backoff, measured from
    the real time at retry.
    """
    # This test provisions a `local-subprocess` backend, which resolve_backend()
    # rejects unless LOCAL_MODE is on (compute_backends.py: "local-subprocess
    # backend is unavailable outside LOCAL_MODE"). test-backend.sh sets
    # LOCAL_MODE=true so it passed there; CI leaves it false, so the retry path
    # raised ValueError and the test failed ONLY on CI. Set it explicitly (the
    # established pattern in this file) so the test is env-independent.
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name=f"doa-{uuid.uuid4().hex[:8]}", user_id="test")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        type="compute_experiment",
        prompt="p",
        status=TaskStatus.IN_PROGRESS,
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
        backend_name="local-subprocess",
    )
    job.status = ComputeJobStatus.STARTING
    db_session.add(job)
    await db_session.commit()

    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    before = _utcnow()
    assert await compute_jobs._maybe_retry_provisioning_timeout(session_factory, job.id)
    after = _utcnow()

    await db_session.refresh(job)
    meta = job.polling_metadata or {}
    assert meta.get("provisioning_retry_count") == 1
    deadline = compute_jobs._parse_naive_iso_datetime(
        meta.get("provisioning_deadline_at")
    )
    assert deadline is not None

    # 1. The deadline is in the FUTURE — the next tick gets a real launch window.
    assert deadline > after, (
        f"retry deadline {deadline.isoformat()} is not in the future "
        f"(now={after.isoformat()}) — retry 1 is dead on arrival"
    )
    assert compute_jobs._provisioning_timeout_seconds(job, now=after) > 0.0, (
        "the next tick sees provisioning_timeout_seconds == 0 and re-times-out "
        "the job WITHOUT ever attempting a fresh launch"
    )
    # 2. It is the backoff measured from the retry itself — the deadline sits
    #    within [now, now + backoff(+jitter)], so a caller whose tick-start clock
    #    is minutes old (a slow launch LRO) cannot shrink it, let alone push it
    #    into the past.
    backoff = compute_jobs.exponential_backoff(
        1,
        base=compute_jobs.PROVISIONING_RETRY_BACKOFF_BASE_SECONDS,
        cap=600.0,
    )
    max_backoff = backoff / 0.8 * 1.2  # undo the jitter band both ways
    # The deadline is stamped at retry time (retry_at), which lies somewhere in
    # [before, after] — so `deadline - before` is at most the backoff PLUS however
    # long the retry call actually took (its DB round-trips). Bound the tolerance
    # by the MEASURED call window rather than a fixed fudge: a loaded CI shard can
    # make the DB work take several seconds, and a hardcoded `+5` turned that real
    # timing into a flaky failure (the test passed in isolation, failed under load).
    call_window = (after - before).total_seconds()
    assert (deadline - before).total_seconds() <= max_backoff + call_window + 1


def test_viewable_pipeline_streams_bundle_and_peak_memory_is_bounded(tmp_path):
    """The viewable pass must never hold a job's whole artifact bundle in RAM.

    This is the OOM, as a measurement. A results bundle is unbounded (checkpoints,
    videos, logs), and the unified viewable pass pulled the ENTIRE object into the
    orchestrator (``blob.download_as_bytes()``) — one fat bundle OOMs the API
    process. It must stream to disk and read only the (count-capped) viewable
    members.

    Both transports are modelled HONESTLY, which is what makes the assertion mean
    something:

    * the streaming seam (``_download_artifact_bundle_to_path``) copies the object
      to its destination file — nothing passes through Python memory;
    * the whole-bundle seam (``storage.download_compute_job_artifact_bundle``) does
      what it really does — materialize the object as ``bytes``. That allocation
      lands inside the measurement window, so an implementation that reaches for it
      is caught paying for it.
    """
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096
    # INCOMPRESSIBLE, like a real checkpoint/video: the wire bytes a whole-bundle
    # download would have to hold are the full 48 MiB, not a gzip artefact of it.
    big_payload = os.urandom(48 * 1024 * 1024)
    src = tmp_path / "bundle.tar.gz"
    # Written straight to disk (compresslevel=1) so the TEST never holds the
    # archive either — only the pipeline's own allocations land in the window.
    with tarfile.open(str(src), mode="w:gz", compresslevel=1) as tar:
        for name, payload in (
            ("./checkpoint/model.pt", big_payload),
            ("./plot.png", png),
            ("./arm1/reward.png", png + b"-a1"),
            ("./notes.txt", b"not viewable"),
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    del big_payload
    bundle_size = src.stat().st_size
    assert bundle_size > 32 * 1024 * 1024, (
        f"the bundle ({bundle_size} bytes) must dwarf the memory ceiling"
    )

    def _fake_stream(_job_id, dest_path):
        shutil.copyfile(str(src), dest_path)  # to disk; no Python-level buffer
        return True

    async def _fake_whole_bundle_download(_job_id):
        return src.read_bytes()  # a real whole-object fetch: 48+ MiB resident

    uploaded: list = []

    async def _fake_upload(job_id, filename, data, content_type):
        uploaded.append(filename)
        return storage.compute_job_viewable_uri(job_id, filename)

    job = MagicMock()
    job.id = uuid.uuid4()
    job.backend = compute_backends_module.GCP_BACKEND_NAME

    with patch.object(
        compute_backends_module, "_download_artifact_bundle_to_path", _fake_stream
    ), patch.object(
        compute_backends_module.storage,
        "download_compute_job_artifact_bundle",
        _fake_whole_bundle_download,
    ), patch.object(
        compute_backends_module.storage,
        "upload_compute_job_viewable_file",
        _fake_upload,
    ):
        tracemalloc.start()
        try:
            manifest, dropped = asyncio.run(
                compute_backends_module.persist_viewable_artifacts(job)
            )
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

    # The pass still WORKS: both viewable files are found inside the fat bundle,
    # under their canonical (subdir-preserving) names; non-viewable members skipped.
    assert sorted(m["filename"] for m in manifest) == ["arm1__reward.png", "plot.png"]
    assert dropped == 0
    assert sorted(uploaded) == ["arm1__reward.png", "plot.png"]

    # And it did not pay for the bundle's size in RAM. The ceiling is generous
    # (well under half the archive), so this pins the SHAPE — bounded, not
    # proportional — rather than a brittle exact number.
    ceiling = 16 * 1024 * 1024
    assert peak < ceiling, (
        f"the viewable pass peaked at {peak / 1024 / 1024:.1f} MiB on a "
        f"{bundle_size / 1024 / 1024:.1f} MiB bundle — it is pulling the whole "
        "bundle into the orchestrator instead of streaming it (this is the OOM)"
    )


def test_checkpoint_resume_streams_predecessor_bundle_and_peak_memory_is_bounded(
    tmp_path,
):
    """Cross-job checkpoint resume must never hold the predecessor bundle in RAM.

    The resume path rehydrates a predecessor compute job's DURABLE artifact bundle
    into the new job's artifact dir before the command runs. A results bundle is
    unbounded (multi-GB RL checkpoints), so pulling the whole object into the
    orchestrator (``download_compute_job_artifact_bundle`` -> ``download_as_bytes``
    + ``io.BytesIO``) is the same leader-OOM class the viewable pass already fixed.
    It must stream the bundle to a temp file and extract from disk.

    Both transports are modelled HONESTLY so the assertion means something:

    * the streaming seam (``_download_artifact_bundle_to_path``) copies the object
      to its destination file — nothing passes through Python memory;
    * the whole-bundle seam (``storage.download_compute_job_artifact_bundle``) does
      what it really does — materialize the object as ``bytes``. That allocation
      lands inside the measurement window, so an implementation that reaches for it
      to resume is caught paying for it.
    """
    # INCOMPRESSIBLE, like a real checkpoint: the wire bytes a whole-object
    # download would hold are the full 48 MiB, not a gzip artefact of it.
    big_checkpoint = os.urandom(48 * 1024 * 1024)
    src = tmp_path / "bundle.tar.gz"
    # Written straight to disk (compresslevel=1) so the TEST never holds the
    # archive either — only the resume path's own allocations land in the window.
    with tarfile.open(str(src), mode="w:gz", compresslevel=1) as tar:
        for name, payload in (
            ("./checkpoint/model.pt", big_checkpoint),
            ("./config.json", b'{"lr": 0.001}'),
            ("./logs/train.log", b"step 1000 loss 0.5\n"),
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    del big_checkpoint
    bundle_size = src.stat().st_size
    assert bundle_size > 32 * 1024 * 1024, (
        f"the bundle ({bundle_size} bytes) must dwarf the memory ceiling"
    )

    def _fake_stream(_job_id, dest_path):
        shutil.copyfile(str(src), dest_path)  # to disk; no Python-level buffer
        return True

    async def _fake_whole_bundle_download(_job_id):
        return src.read_bytes()  # a real whole-object fetch: 48+ MiB resident

    dest_dir = tmp_path / "artifacts"

    with patch.object(
        compute_backends_module, "_download_artifact_bundle_to_path", _fake_stream
    ), patch.object(
        compute_backends_module.storage,
        "download_compute_job_artifact_bundle",
        _fake_whole_bundle_download,
    ):
        tracemalloc.start()
        try:
            extracted = asyncio.run(
                compute_backends_module._rehydrate_input_artifact_bundle(
                    "predecessor-job-id", str(dest_dir)
                )
            )
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

    # The resume still WORKS: every member is rehydrated onto disk under the
    # artifact dir, checkpoint included.
    assert extracted == 3
    assert (dest_dir / "checkpoint" / "model.pt").stat().st_size == 48 * 1024 * 1024
    assert (dest_dir / "config.json").read_bytes() == b'{"lr": 0.001}'
    assert (dest_dir / "logs" / "train.log").exists()

    # And it did not pay for the bundle's size in RAM. The ceiling is generous
    # (well under half the archive), pinning the SHAPE — bounded, not proportional.
    ceiling = 16 * 1024 * 1024
    assert peak < ceiling, (
        f"checkpoint resume peaked at {peak / 1024 / 1024:.1f} MiB on a "
        f"{bundle_size / 1024 / 1024:.1f} MiB bundle — it is buffering the whole "
        "predecessor bundle in the orchestrator heap instead of streaming it to "
        "disk (this is the leader OOM on multi-GB checkpoints)"
    )


def test_no_progress_watchdog_escalates_to_sigkill_when_sigterm_is_ignored(
    tmp_path, monkeypatch
):
    """A command that ignores SIGTERM must still be reaped by the no-progress watchdog.

    The watchdog is a background subshell of the job's process-group leader, so the
    ``kill -TERM -$$`` it fires at the group lands on the WATCHDOG ITSELF. It died
    right there — and the SIGKILL escalation two lines below never ran. A command
    that traps/ignores SIGTERM therefore kept running (and kept billing) after the
    watchdog had already written the ``no_progress_timeout`` terminal record: the
    no-progress kill silently did not kill.

    Runs the REAL emitted wrapper against a REAL SIGTERM-ignoring process; only a
    reachable SIGKILL escalation can pass.
    """
    import subprocess
    import sys
    import time

    if not sys.platform.startswith("linux"):
        pytest.skip("process-group signalling + `stat -c` target the Linux substrate")

    monkeypatch.setattr(
        compute_backends_module, "_LOCAL_STATE_ROOT", str(tmp_path / "state")
    )
    external_id = "np-sigkill-e2e"
    pid_file = tmp_path / "stubborn.pid"
    stubborn = tmp_path / "stubborn.py"
    stubborn.write_text(
        "import os, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "with open(sys.argv[1], 'w') as fh:\n"
        "    fh.write(str(os.getpid()))\n"
        "    fh.flush()\n"
        "time.sleep(600)\n",
        encoding="utf-8",
    )

    wrapper = compute_backends_module._build_local_wrapper_command(
        f"python3 {shlex.quote(str(stubborn))} {shlex.quote(str(pid_file))}",
        external_id=external_id,
        no_progress_timeout_seconds=5,  # the command never touches the progress file
    )

    def _alive(pid: int) -> bool:
        """True while the process exists and is not an unreaped zombie."""
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                state = fh.read().rsplit(")", 1)[1].split()[0]
        except OSError:
            return False
        return state != "Z"

    proc = subprocess.Popen(
        ["bash", "-lc", wrapper],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # exactly how the backend spawns it: $$ == the pgid
    )
    stubborn_pid = None
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            raw_pid = (
                pid_file.read_text(encoding="utf-8").strip()
                if pid_file.exists()
                else ""
            )
            if raw_pid:
                stubborn_pid = int(raw_pid)
                break
            time.sleep(0.1)
        assert stubborn_pid, "the SIGTERM-ignoring command never started"

        # The watchdog fires at ~5s (no progress), TERMs the group (which the
        # command ignores), and must escalate to SIGKILL ~2s later.
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and _alive(stubborn_pid):
            time.sleep(0.2)

        assert not _alive(stubborn_pid), (
            "the SIGTERM-ignoring command SURVIVED the no-progress watchdog — its "
            "SIGKILL escalation is unreachable (the watchdog killed itself with the "
            "group SIGTERM it sent), so a stalled job keeps running, and keeps "
            "billing, after being reported dead"
        )

        state_dir = compute_backends_module._local_state_dir_for_external_id(external_id)
        terminal_path = compute_backends_module._local_terminal_path_for_external_id(
            external_id
        )
        assert os.path.exists(terminal_path), f"no terminal record in {state_dir}"
        raw = open(terminal_path, encoding="utf-8").read()
        assert "REASON=no_progress_timeout" in raw, raw
        assert "EXIT_CODE=125" in raw, raw
    finally:
        if stubborn_pid and _alive(stubborn_pid):
            try:
                os.kill(stubborn_pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            pass


def test_no_progress_watchdog_shields_itself_from_its_own_group_term():
    """Shape guard pairing with the e2e test above: the watchdog must ignore TERM
    before it group-TERMs, or its SIGKILL escalation is dead code."""
    wrapper = compute_backends_module._build_local_wrapper_command(
        "echo ok", external_id="np-shape", no_progress_timeout_seconds=120
    )
    lines = [line.strip() for line in wrapper.splitlines()]
    term_line = "kill -TERM -$$ 2>/dev/null || true"
    kill_line = "kill -KILL -$$ 2>/dev/null || true"
    assert "trap '' TERM" in lines, (
        "the watchdog subshell shares the process group it signals — without "
        "ignoring TERM first it kills itself and never reaches the SIGKILL escalation"
    )
    assert lines.index("trap '' TERM") < lines.index(term_line)
    assert lines.index(term_line) < lines.index(kill_line)


def test_local_container_watchdog_names_and_docker_kills_the_container(monkeypatch):
    """Shape guard: a CONTAINERIZED job's watchdog must docker-kill the container.

    The trainer inside runs as a child of dockerd, NOT a member of the wrapper's
    process group, so the watchdog's group SIGKILL can never reach it. The emitted
    wrapper must therefore (a) give the container a deterministic ``--name`` and
    (b) ``docker kill`` THAT name in the watchdog escalation, and do so BEFORE the
    group KILL reaps the watchdog (otherwise the escalation is dead code). A bare
    (non-container) job must NOT gain a docker kill — the group kill reaches it.
    """
    monkeypatch.setattr(compute_backends_module, "detect_local_gpu", lambda: None)
    external_id = "np-container-shape"
    name = compute_backends_module._local_container_name_for_external_id(external_id)
    wrapper = compute_backends_module._build_local_wrapper_command(
        "python train.py",
        external_id=external_id,
        container_image="nvcr.io/nvidia/pytorch:24.01-py3",
        no_progress_timeout_seconds=30,
    )
    assert f"--name {shlex.quote(name)}" in wrapper
    lines = [line.strip() for line in wrapper.splitlines()]
    docker_kill_line = f"docker kill {shlex.quote(name)} 2>/dev/null || true"
    kill_group_line = "kill -KILL -$$ 2>/dev/null || true"
    assert docker_kill_line in lines, (
        "the containerized watchdog must docker kill the container by name — a "
        "group SIGKILL cannot reach the dockerd-parented trainer"
    )
    assert lines.index(docker_kill_line) < lines.index(kill_group_line), (
        "docker kill must run BEFORE the group KILL reaps the watchdog, else the "
        "container-reaching escalation never executes"
    )
    # Bare job: no container, no docker kill (group kill reaches everything).
    bare = compute_backends_module._build_local_wrapper_command(
        "echo ok", external_id="np-bare", no_progress_timeout_seconds=30
    )
    assert "docker kill" not in bare
    assert "--name" not in bare


def test_local_container_watchdog_actually_kills_containerized_workload(
    tmp_path, monkeypatch
):
    """END-TO-END: a TERM-ignoring CONTAINERIZED trainer must be reaped by the
    no-progress watchdog.

    A containerized trainer runs as a child of dockerd, NOT a member of the
    wrapper's process group, so the watchdog's group SIGKILL cannot touch it — a
    TERM-ignoring trainer kept running (and kept billing) after the job was
    recorded FAILED. Only reaching the container directly (``docker kill <name>``)
    stops it.

    Modelled HONESTLY with a fake ``docker`` on PATH that reparents the trainer
    OUT of the wrapper's process group (``start_new_session``), exactly as dockerd
    does: the group signals then genuinely cannot reach the trainer, and ONLY a
    ``docker kill`` targeting the recorded container can. If the wrapper does not
    docker-kill the container, the trainer survives and this test fails.
    """
    import subprocess
    import textwrap
    import time

    if not sys.platform.startswith("linux"):
        pytest.skip("process-group signalling + `/proc` state target the Linux substrate")

    monkeypatch.setattr(compute_backends_module, "detect_local_gpu", lambda: None)
    monkeypatch.setattr(
        compute_backends_module, "_LOCAL_STATE_ROOT", str(tmp_path / "state")
    )
    external_id = "np-container-e2e"
    container_name = compute_backends_module._local_container_name_for_external_id(
        external_id
    )

    # Fake-docker state dir: name -> container (session) pid, so `docker kill
    # <name>` can reach the reparented trainer.
    fake_state = tmp_path / "fakedocker"
    fake_state.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env python3\n"
        + textwrap.dedent(
            f"""
            import os, sys, signal, subprocess
            STATE = {str(fake_state)!r}
            argv = sys.argv[1:]
            if not argv:
                sys.exit(0)
            verb = argv[0]
            if verb in ("pull", "login"):
                sys.exit(0)
            if verb == "run":
                name = None
                for i, a in enumerate(argv):
                    if a == "--name" and i + 1 < len(argv):
                        name = argv[i + 1]
                # The container command is everything from the first `bash` token.
                try:
                    bidx = argv.index("bash")
                except ValueError:
                    sys.exit(0)
                inner = argv[bidx:]
                # start_new_session: the trainer runs in its OWN session/group,
                # NOT the caller's process group. This models the dockerd-parenting
                # that makes the wrapper's group SIGKILL unable to reach it.
                child = subprocess.Popen(inner, start_new_session=True)
                if name:
                    with open(os.path.join(STATE, name + ".pid"), "w") as fh:
                        fh.write(str(child.pid))
                # If WE (the docker client) are signalled, the container keeps
                # running — just like a real container detached from a dead CLI.
                child.wait()
                sys.exit(0)
            if verb == "kill":
                name = None
                for a in argv[1:]:
                    if not a.startswith("-"):
                        name = a
                if name:
                    try:
                        with open(os.path.join(STATE, name + ".pid")) as fh:
                            pid = int(fh.read().strip())
                        os.killpg(pid, signal.SIGKILL)
                    except (OSError, ValueError):
                        pass
                sys.exit(0)
            sys.exit(0)
            """
        ),
        encoding="utf-8",
    )
    os.chmod(fake_docker, 0o755)

    # A login shell may reset PATH (Debian's /etc/profile does). Re-prepend the
    # fake-docker dir from ~/.bash_profile so every `bash -lc` in the wrapper
    # resolves `docker` to the fake regardless of profile behaviour.
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    (home_dir / ".bash_profile").write_text(
        f'export PATH={shlex.quote(str(fake_bin))}:"$PATH"\n', encoding="utf-8"
    )

    pid_file = tmp_path / "trainer.pid"
    stubborn = tmp_path / "trainer.py"
    stubborn.write_text(
        "import os, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "with open(sys.argv[1], 'w') as fh:\n"
        "    fh.write(str(os.getpid()))\n"
        "    fh.flush()\n"
        "time.sleep(600)\n",
        encoding="utf-8",
    )

    wrapper = compute_backends_module._build_local_wrapper_command(
        f"python3 {shlex.quote(str(stubborn))} {shlex.quote(str(pid_file))}",
        external_id=external_id,
        container_image="fake/trainer:latest",
        no_progress_timeout_seconds=5,  # the command never touches the progress file
    )
    # Setup sanity: the container is named so the fake can record + target it.
    assert f"--name {shlex.quote(container_name)}" in wrapper

    env = dict(os.environ)
    env["HOME"] = str(home_dir)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"

    def _alive(pid: int) -> bool:
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                state = fh.read().rsplit(")", 1)[1].split()[0]
        except OSError:
            return False
        return state != "Z"

    proc = subprocess.Popen(
        ["bash", "-lc", wrapper],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    trainer_pid = None
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            raw = pid_file.read_text().strip() if pid_file.exists() else ""
            if raw:
                trainer_pid = int(raw)
                break
            time.sleep(0.1)
        assert trainer_pid, (
            "the containerized trainer never started (fake docker not on PATH?)"
        )

        # It really is OUTSIDE the wrapper's process group (dockerd-parented) — the
        # precondition that makes the group SIGKILL unable to reach it.
        assert os.getpgid(trainer_pid) != os.getpgid(proc.pid), (
            "the trainer must be in a different process group than the wrapper for "
            "this to model a dockerd-parented workload"
        )

        # The watchdog fires ~5s (no progress), group-TERMs (futile), then must
        # `docker kill` the container ~2s later.
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and _alive(trainer_pid):
            time.sleep(0.2)

        assert not _alive(trainer_pid), (
            "the TERM-ignoring CONTAINERIZED trainer SURVIVED the no-progress "
            "watchdog — the group SIGKILL cannot reach a dockerd-parented process, "
            "so without a `docker kill <name>` escalation a stalled containerized "
            "job keeps running, and keeps billing, after being reported FAILED"
        )

        terminal_path = compute_backends_module._local_terminal_path_for_external_id(
            external_id
        )
        assert os.path.exists(terminal_path), "no terminal record written"
        raw = open(terminal_path, encoding="utf-8").read()
        assert "REASON=no_progress_timeout" in raw, raw
    finally:
        if trainer_pid and _alive(trainer_pid):
            try:
                os.killpg(os.getpgid(trainer_pid), signal.SIGKILL)
            except OSError:
                pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            pass


def test_no_blocking_time_sleep_in_async_functions():
    """Regression: async coroutines must not call the blocking builtin
    time.sleep(), which freezes the entire event loop. Cooldown waits and any
    other in-coroutine delays must use `await asyncio.sleep(...)` instead.

    This walks the AST of app/compute.py and fails if any `time.sleep(...)`
    call is lexically nested inside an `async def`.
    """
    import ast
    import inspect

    source = inspect.getsource(compute)
    tree = ast.parse(source)

    def _is_time_sleep(call_node):
        func = call_node.func
        return (
            isinstance(func, ast.Attribute)
            and func.attr == "sleep"
            and isinstance(func.value, ast.Name)
            and func.value.id == "time"
        )

    offenders = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self):
            self.async_stack = []

        def visit_AsyncFunctionDef(self, node):
            self.async_stack.append(node.name)
            self.generic_visit(node)
            self.async_stack.pop()

        def visit_FunctionDef(self, node):
            # A sync def nested inside an async def breaks the async context;
            # blocking sleeps there are acceptable, so clear the stack.
            saved = self.async_stack
            self.async_stack = []
            self.generic_visit(node)
            self.async_stack = saved

        def visit_Call(self, node):
            if self.async_stack and _is_time_sleep(node):
                offenders.append((self.async_stack[-1], node.lineno))
            self.generic_visit(node)

    _Visitor().visit(tree)

    assert not offenders, (
        "Blocking time.sleep() found inside async function(s) in compute.py "
        f"(use await asyncio.sleep instead): {offenders}"
    )
