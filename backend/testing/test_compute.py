"""
Tests for app/compute.py

Covers:
- _store_agent_secret IAM policy ETag retry + idempotency.
- Zone success-rate tracking (rolling window, zone ordering, reset, summary).
"""

import asyncio
import base64
import json
from collections import deque
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import os
import signal
import uuid

import pytest
from google.api_core.exceptions import Aborted
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app import compute
from app import compute_backends as compute_backends_module
from app import compute_jobs
from app import provision_error_scope
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
    ComputeJob,
    ComputeJobOutcome,
    ComputeJobStatus,
    Project,
    Task,
    TaskStatus,
)


class _FakeBinding:
    """Mimics the subset of policy_pb2.Binding used by the idempotency check."""

    def __init__(self, role: str, members):
        self.role = role
        self.members = list(members)


class _FakePolicy:
    """Mimics the subset of the IAM policy proto used by _store_agent_secret."""

    def __init__(self, bindings=None):
        self.bindings = list(bindings or [])


def _make_client(
    existing_policy_bindings=None,
    set_iam_side_effects=None,
    payload: bytes | None = None,
):
    """Build a MagicMock SecretManagerServiceClient with sensible defaults.

    - create_secret returns an object whose .name looks like a real secret resource
    - add_secret_version returns an object whose .name looks like a version resource
    - get_iam_policy returns a fresh _FakePolicy with the given bindings on every call
    - set_iam_policy applies set_iam_side_effects in order (e.g. Aborted exceptions)
    - access_secret_version (audit E15: post-create verification) returns the
      payload that was supplied via ``payload=`` so the verify branch sees a
      match. Tests that don't pass payload= will get an empty bytes payload
      and trip the verification mismatch — which is the correct behavior to
      cover the failure path.
    """
    client = MagicMock()
    secret_obj = MagicMock()
    secret_obj.name = "projects/test-proj/secrets/agent-creds-vm123"
    client.create_secret.return_value = secret_obj

    version_obj = MagicMock()
    version_obj.name = "projects/test-proj/secrets/agent-creds-vm123/versions/1"
    client.add_secret_version.return_value = version_obj

    # get_iam_policy returns a fresh policy each call so retries re-read it
    client.get_iam_policy.side_effect = lambda request: _FakePolicy(
        bindings=[
            _FakeBinding(b.role, b.members) for b in (existing_policy_bindings or [])
        ]
    )

    if set_iam_side_effects is not None:
        client.set_iam_policy.side_effect = set_iam_side_effects

    # E15 verification path: read-back must match what was written.
    verify_obj = MagicMock()
    verify_obj.payload.data = payload if payload is not None else b""
    client.access_secret_version.return_value = verify_obj

    return client


@pytest.fixture()
def patched_client():
    """Patch the secretmanager client factory inside _store_agent_secret.

    The real imports happen lazily inside the function, so we patch the module
    that gets imported at call time.
    """
    with patch("google.cloud.secretmanager.SecretManagerServiceClient") as factory:
        client = _make_client()
        factory.return_value = client
        yield client


class TestStoreAgentSecretIamRetry:
    """Retry + idempotency tests for _store_agent_secret IAM policy handling."""

    def test_retries_on_aborted_and_succeeds(self, monkeypatch):
        """set_iam_policy raises Aborted twice then succeeds; function returns version name.

        Expects the get/set read-modify-write loop to be retried with a fresh
        get_iam_policy each iteration (ETag-correct behavior).
        """
        # Don't actually sleep during retries
        monkeypatch.setattr(compute.time, "sleep", lambda _s: None)

        # First two set_iam_policy calls raise Aborted, third succeeds.
        side_effects = [
            Aborted("etag mismatch 1"),
            Aborted("etag mismatch 2"),
            None,
        ]
        payload = '{"token": "abc"}'
        client = _make_client(
            set_iam_side_effects=side_effects, payload=payload.encode("utf-8")
        )

        with patch(
            "google.cloud.secretmanager.SecretManagerServiceClient", return_value=client
        ):
            result = compute._store_agent_secret("vm123", payload)

        assert result == "projects/test-proj/secrets/agent-creds-vm123/versions/1"
        assert client.set_iam_policy.call_count == 3
        # Each retry must re-read the policy to get a fresh ETag
        assert client.get_iam_policy.call_count == 3

    def test_idempotent_skip_when_sa_already_bound(self):
        """If the agent SA is already bound for secretAccessor, skip set_iam_policy.

        Prevents duplicate bindings when a secret is reused (e.g. AlreadyExists
        catch at the top of the function).
        """
        agent_member = f"serviceAccount:{compute.AGENT_SERVICE_ACCOUNT_EMAIL}"
        existing = [
            _FakeBinding(
                role="roles/secretmanager.secretAccessor",
                members=[agent_member],
            ),
        ]
        payload = '{"token": "abc"}'
        client = _make_client(
            existing_policy_bindings=existing, payload=payload.encode("utf-8")
        )

        with patch(
            "google.cloud.secretmanager.SecretManagerServiceClient", return_value=client
        ):
            result = compute._store_agent_secret("vm123", payload)

        assert result == "projects/test-proj/secrets/agent-creds-vm123/versions/1"
        # Policy was read once to check the binding, then skipped.
        assert client.get_iam_policy.call_count == 1
        assert client.set_iam_policy.call_count == 0

    def test_aborted_exhausts_retries_and_raises(self, monkeypatch):
        """If Aborted fires max_retries (5) times, the final attempt re-raises Aborted."""
        monkeypatch.setattr(compute.time, "sleep", lambda _s: None)

        # Supply one more than max_retries (5) to prove we never exceed.
        side_effects = [Aborted(f"etag mismatch {i}") for i in range(6)]
        client = _make_client(set_iam_side_effects=side_effects)

        with (
            patch(
                "google.cloud.secretmanager.SecretManagerServiceClient",
                return_value=client,
            ),
            pytest.raises(Aborted),
        ):
            compute._store_agent_secret("vm123", '{"token": "abc"}')

        assert client.set_iam_policy.call_count == 5
        assert client.get_iam_policy.call_count == 5


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


def _metrics_marker(payload: object) -> str:
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def test_gcp_serial_parse_extracts_result_metrics():
    marker = _metrics_marker({"accuracy": 0.91, "tokens_per_sec": 1234, "ok": True})
    serial = "\n".join(
        [
            "BLUEPRINT_COMPUTE_REASON=completed",
            "BLUEPRINT_COMPUTE_STATUS=SUCCEEDED",
            "BLUEPRINT_COMPUTE_EXIT_CODE=0",
            f"BLUEPRINT_COMPUTE_METRICS={marker}",
        ]
    )
    status = compute_backends_module._parse_serial_status(serial)
    assert status is not None
    assert status.status == ComputeJobStatus.SUCCEEDED
    # bool coerced to a number; all values numeric.
    assert status.metrics == {"accuracy": 0.91, "tokens_per_sec": 1234.0, "ok": 1.0}


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
    serial = "\n".join(
        [
            "BLUEPRINT_COMPUTE_STATUS=SUCCEEDED",
            "BLUEPRINT_COMPUTE_EXIT_CODE=0",
            "BLUEPRINT_COMPUTE_METRICS=not-valid-base64-$$$",
        ]
    )
    status = compute_backends_module._parse_serial_status(serial)
    assert status is not None
    assert status.status == ComputeJobStatus.SUCCEEDED
    assert status.metrics is None


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

    class FakeBackend:
        def external_id_for(self, persisted_job):
            return "fake-job-metrics"

        async def launch(self, spec):
            return ComputeLaunchResult(
                external_id="fake-job-metrics",
                started_at=datetime.utcnow() - timedelta(seconds=10),
                cost_rate_per_second=0.0,
                message="launched",
            )

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.01, measured_at=as_of)

        async def poll_status(self, persisted_job):
            return ComputeStatusResult(
                status=ComputeJobStatus.SUCCEEDED,
                exit_code=0,
                finished_at=datetime.utcnow(),
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

    started_at = datetime.utcnow() - timedelta(seconds=30)

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
                finished_at=datetime.utcnow(),
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
async def test_create_compute_job_rejects_gpu_machine_family_mismatch(
    db_session, monkeypatch
):
    """A GPU paired with a mismatched machine family (T4 + g2) is rejected."""
    monkeypatch.setattr(compute, "LOCAL_MODE", False)
    task = await _make_compute_task(db_session)
    with pytest.raises(ValueError, match="not a valid single-GPU shape"):
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py",
            machine_type="g2-standard-4",
            spend_cap_usd=5.0,
            deadline_seconds=600,
            backend_name="gcp",
            accelerator="T4",
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
    job.started_at = datetime.utcnow() - timedelta(seconds=60)
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
    job.started_at = datetime.utcnow() - timedelta(seconds=60)
    job.wall_clock_deadline = datetime.utcnow() - timedelta(seconds=1)
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
    assert "deadline" in (job.terminal_reason or "")


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

    now = datetime.utcnow()

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

    jobs = await compute_jobs.list_compute_jobs(
        db_session, project_id=project.id, include_finished_within=timedelta(hours=6)
    )
    job_ids = {j.id for j in jobs}
    assert active.id in job_ids
    assert recent_done.id in job_ids
    assert stale_done.id not in job_ids  # finished outside the window
    assert other_active.id not in job_ids  # different project


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

    def test_zero_failures_baseline(self):
        assert compute._zone_cooldown_seconds(0) == compute.ZONE_COOLDOWN_SECONDS

    def test_doubling_on_each_failure(self):
        base = compute.ZONE_COOLDOWN_SECONDS
        assert compute._zone_cooldown_seconds(1) == min(base * 2, 1800)
        assert compute._zone_cooldown_seconds(2) == min(base * 4, 1800)
        assert compute._zone_cooldown_seconds(3) == min(base * 8, 1800)

    def test_cap_at_1800_seconds(self):
        assert compute._zone_cooldown_seconds(10) == 1800
        assert compute._zone_cooldown_seconds(100) == 1800

    def test_monotonically_non_decreasing(self):
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

    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

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
