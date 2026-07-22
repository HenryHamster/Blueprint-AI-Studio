"""
Shard of test_compute.py: hardware policy enforcement and compute-experiment
endpoint tests.

Covers:
- Hardware policy enforcement in create_compute_job (allowed_machine_types,
  allowed_accelerator_types).
- Compute-experiment preflight endpoint (POST /api/admin/tasks/compute-experiment/preflight).
- Compute-experiment launch policy gate (at-launch hardware policy check).
- Compute-experiment forced runner pin (runner_target_id resolution).
- Live output tail delivery (heartbeat + local backend reading).
- Local-runner-required policy enforcement (TestCreateComputeJobRequireLocalRunner).
"""

import base64
import json
import shlex
import uuid
from datetime import datetime, timedelta

import pytest

from app import compute
from app import compute_backends as compute_backends_module
from app import compute_jobs
from app.compute_backends import ComputeLaunchSpec
from testing._compute_test_helpers import _make_compute_task
from app.models import (
    ComputeJob,
    ComputeJobStatus,
    Project,
    ProjectMember,
    Task,
    TaskStatus,
)

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
                meta,
                metric="loss",
                value=v,
                t=t.isoformat(),
                received_at=t,
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
            meta = append_progress_sample(
                meta,
                metric="loss",
                value=v,
                t=t.isoformat(),
                received_at=t,
            )
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
            global_job_meta,
            metric="loss",
            value=v,
            t=t.isoformat(),
            received_at=t,
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
    from app.field_encryption import decrypt_field

    persisted_auth = (job.polling_metadata or {}).get("container_registry_auth")
    assert persisted_auth is not None
    assert persisted_auth["server"] == "nvcr.io"
    assert persisted_auth["username"] == "$oauthtoken"
    # Password is stored encrypted; decrypt to verify the round-trip.
    assert decrypt_field(persisted_auth["password"]) == "s3cr3t-ngc-api-key"


@pytest.mark.asyncio
async def test_create_compute_job_registry_auth_password_encrypted_at_rest(
    db_session, monkeypatch
):
    """Registry password must be stored as Fernet ciphertext when a real key is configured."""
    from app import auth as auth_mod
    from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME
    from app.field_encryption import FERNET_CIPHERTEXT_PREFIX, decrypt_field
    from app.models import LocalRunner
    from cryptography.fernet import Fernet

    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    # Use a real Fernet key so encrypt_field produces actual ciphertext.
    real_key = Fernet.generate_key().decode()
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", real_key)
    monkeypatch.delenv("ALLOW_PLAINTEXT_CREDENTIALS", raising=False)

    project = Project(name="Registry Auth Encrypt Test", user_id="u")
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
    # Password must be stored as Fernet ciphertext, not plaintext.
    assert persisted_auth["password"] != "s3cr3t-ngc-api-key"
    assert persisted_auth["password"].startswith(FERNET_CIPHERTEXT_PREFIX)
    # Decryption must recover the original value.
    assert decrypt_field(persisted_auth["password"]) == "s3cr3t-ngc-api-key"


def test_decrypt_registry_auth_decrypts_password(monkeypatch):
    """_decrypt_registry_auth must decrypt a Fernet-encrypted password field."""
    from app.routers.runner import _decrypt_registry_auth
    from app.field_encryption import encrypt_field, FERNET_CIPHERTEXT_PREFIX
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)
    monkeypatch.delenv("ALLOW_PLAINTEXT_CREDENTIALS", raising=False)

    encrypted = encrypt_field("secret-pw")
    assert encrypted is not None and encrypted.startswith(FERNET_CIPHERTEXT_PREFIX)
    auth = {"server": "nvcr.io", "username": "$oauthtoken", "password": encrypted}
    result = _decrypt_registry_auth(auth)
    assert result is not None
    assert result["password"] == "secret-pw"
    assert result["server"] == "nvcr.io"
    assert result["username"] == "$oauthtoken"


def test_decrypt_registry_auth_handles_no_password():
    """_decrypt_registry_auth must pass through auth dicts without a password field."""
    from app.routers.runner import _decrypt_registry_auth

    auth_no_pw = {"server": "nvcr.io", "username": "$oauthtoken"}
    result = _decrypt_registry_auth(auth_no_pw)
    assert result == auth_no_pw

    assert _decrypt_registry_auth(None) is None


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

