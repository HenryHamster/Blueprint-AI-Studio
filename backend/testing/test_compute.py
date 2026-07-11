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
from collections import deque
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
from app import schemas
from app import storage
from app.compute_backends import (
    ComputeCostResult,
    GcpComputeBackend,
    ComputeLaunchResult,
    ComputeLaunchSpec,
    LocalSubprocessComputeBackend,
    ComputeStatusResult,
)
from app.database import resolve_async_engine
from app.models import (
    Alert,
    AlertSeverity,
    Artifact,
    ArtifactType,
    ComputeJob,
    ComputeJobOutcome,
    ComputeJobStatus,
    KnowledgeDocument,
    Project,
    ProjectMember,
    ReleaseDeployArtifact,
    ReleaseVersion,
    Task,
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
                    KnowledgeDocument.source_task_id == task.id
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
                    KnowledgeDocument.source_task_id == task.id
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
    assert docs[0].source_task_id == task.id

    # Third sweep: no marker, no new work — idempotent
    third = await sweep_deliver_task_wakes(db_session, _utcnow())
    assert third == 0
    db_session.expire_all()
    docs_again = (
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

    # --- Build batch_size DELIVERED rows WITHOUT the marker ---
    # Use the default batch_size of sweep_deliver_task_wakes (10).
    batch_size = 10
    for _ in range(batch_size):
        filler_task = Task(
            project_id=project.id,
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
        # pending post-commit side effects.
        db_session.expire_all()
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
        meta = dict(filler_reg.source_metadata or {})
        meta.pop(_COMPUTE_WAKE_POST_COMMIT_META_KEY, None)
        filler_reg.source_metadata = meta
        db_session.add(filler_reg)
        await db_session.commit()

    # --- Build ONE DELIVERED row WITH the marker ---
    marked_task = Task(
        project_id=project.id,
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

    # Deliver the marked row — marker is seeded atomically at delivery time.
    marked_reg = (
        (
            await db_session.execute(
                select(TaskWakeRegistration).where(
                    TaskWakeRegistration.wake_source_ref == str(marked_job.id)
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
                    TaskWakeRegistration.wake_source_ref == str(marked_job.id)
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
                    TaskWakeRegistration.wake_source_ref == str(marked_job.id)
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
                    KnowledgeDocument.source_task_id == marked_task.id
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
    assert docs[0].source_task_id == marked_task.id


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

    batch_size = 10
    # --- Build batch_size DELIVERED rows WITH the marker AND a far-future backoff ---
    far_future = (_utcnow() + timedelta(hours=24)).isoformat()
    backlog_regs = []
    for _ in range(batch_size):
        backlog_task = Task(
            project_id=project.id,
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

        # Inject a far-future backoff timestamp so this row is skipped by the sweep.
        db_session.expire_all()
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
        meta = dict(b_reg.source_metadata or {})
        meta[_COMPUTE_WAKE_NEXT_RETRY_KEY] = far_future
        b_reg.source_metadata = meta
        db_session.add(b_reg)
        await db_session.commit()
        backlog_regs.append(b_reg.id)

    # --- Build ONE DELIVERED row WITH the marker and WITHOUT next_retry_after ---
    ready_task = Task(
        project_id=project.id,
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

    ready_reg = (
        (
            await db_session.execute(
                select(TaskWakeRegistration).where(
                    TaskWakeRegistration.wake_source_ref == str(ready_job.id)
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
                    TaskWakeRegistration.wake_source_ref == str(ready_job.id)
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
                    TaskWakeRegistration.wake_source_ref == str(ready_job.id)
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
                    KnowledgeDocument.source_task_id == ready_task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(docs) == 1
    assert docs[0].source_task_id == ready_task.id

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
        "e2-standard-2",
        "e2-standard-4",
        "e2-standard-8",
        "e2-standard-16",
        "e2-standard-32",
    }
    assert all(mt.startswith("e2-") for mt in vcr.supported_machine_types())
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

USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


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
    await db_session.refresh(task1)
    db_session.expire_all()
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

    db_session.expire_all()
    await db_session.refresh(task)
    reg_fresh = await db_session.get(TaskWakeRegistration, reg.id)

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
    reg_again = await db_session.get(TaskWakeRegistration, reg.id)
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
        job_id=job.id,
        project_id=project.id,
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
            project_id=project.id,
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
        (await db_session.execute(select(Alert).where(Alert.project_id == project.id)))
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
                    Alert.project_id == project.id,
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
                    Alert.project_id == project.id,
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
    # The setup step runs before the main command (python3 - <<'PY' block).
    setup_idx = wrapper.index("pip install torch")
    cmd_idx = wrapper.index("python3 -")
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
async def test_compute_job_vm_heartbeat_missing_job_returns_404(client, monkeypatch):
    """POST /compute-jobs/{id}/heartbeat for an unknown ID returns 404."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    fake_id = uuid.uuid4()
    resp = await client.post(
        f"/api/agent/compute-jobs/{fake_id}/heartbeat",
        headers={"Authorization": f"Bearer {fake_id}"},
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
        headers={"Authorization": f"Bearer {job.id}"},
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
        headers={"Authorization": f"Bearer {job.id}"},
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
        headers={"Authorization": f"Bearer {job.id}"},
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
            "Authorization": f"Bearer {job.id}",
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
        headers={"Authorization": f"Bearer {job.id}"},
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


def test_gcp_startup_script_heartbeat_reads_live_tail():
    """The _compute_heartbeat function in the startup script must read live_tail.b64."""
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
    hb_func_start = script.index("_compute_heartbeat() {")
    hb_func_body = script[hb_func_start : hb_func_start + 600]
    assert "live_tail.b64" in hb_func_body, (
        "_compute_heartbeat must reference live_tail.b64"
    )
    assert "live_output_tail" in hb_func_body, (
        "_compute_heartbeat must include live_output_tail in the POST body"
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

    async def _fake_attach_repo_clone(session_factory, job, spec):
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
async def test_retry_provisioning_timeout_extends_deadline(db_session):
    """``_maybe_retry_provisioning_timeout`` extends the provisioning deadline
    and increments the retry counter when retry budget remains."""
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
    now = _utcnow()

    # First retry should succeed.
    result = await compute_jobs._maybe_retry_provisioning_timeout(
        session_factory, job.id, now
    )
    assert result is True

    await db_session.refresh(job)
    meta = job.polling_metadata or {}
    assert meta.get("provisioning_retry_count") == 1
    assert "provisioning_deadline_at" in meta
    assert "retry 1/2" in meta.get("status_message", "")


@pytest.mark.asyncio
async def test_retry_provisioning_timeout_exhausted(db_session):
    """``_maybe_retry_provisioning_timeout`` returns False when max retries have
    been consumed, so the caller proceeds with the terminal transition."""
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
        session_factory, job.id, _utcnow()
    )
    assert result is False


@pytest.mark.asyncio
async def test_retry_provisioning_timeout_already_terminal(db_session):
    """``_maybe_retry_provisioning_timeout`` returns True (no-op) when the job
    is already terminal (concurrent race)."""
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
        session_factory, job.id, _utcnow()
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
