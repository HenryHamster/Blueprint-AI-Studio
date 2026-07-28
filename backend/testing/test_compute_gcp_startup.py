"""
Tests for GCP startup script and serial-parse behavior in app/compute_backends.py.

Covers:
- GCP serial output parsing (metrics, status, command-log markers).
- GCP startup script generation (durable terminal record, watchdog, resume block,
  env validation, viewable watcher, artifact upload, GPU/container variants).
- GCP backend launch, terminate, and adopt-existing-VM flows.
- GCP poll_status reconciliation from durable terminal records.
- _attach_repo_clone and _build_launch_spec repo-clone path.
- _provision_local secret-isolation guard.
- parse_serial_status release-echo and resume-marker parsing.
"""

import base64
import io
import json
import re
import shutil
import tarfile
import uuid
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import compute
from app import compute_backends as compute_backends_module
from app import compute_jobs
from app import storage
from app.compute_backends import (
    ComputeLaunchSpec,
    GcpComputeBackend,
)
from app.database import resolve_async_engine
from testing._compute_test_helpers import (
    _make_tar_gz_bundle,
    _terminal_record_bytes,
)
from app.models import (
    ComputeJob,
    ComputeJobStatus,
    Project,
    Task,
    TaskStatus,
)

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


def test_decode_result_metrics_preserves_scalar_diagnostics_and_bounds():
    # Strings, numbers, and bools survive; nulls and nested structures drop.
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
        "label": "high",
        "flag": 0.0,
    }
    # A non-object JSON payload (array) is rejected entirely.
    assert (
        compute_backends_module._decode_result_metrics(_metrics_marker([1, 2, 3]))
        is None
    )
    # A string-only result preserves a useful diagnostic rather than vanishing.
    assert compute_backends_module._decode_result_metrics(_metrics_marker({"x": "y"})) == {
        "x": "y"
    }
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


def _run_gcp_resume_block(
    monkeypatch, tmp_path, *, members: dict[str, bytes], fail_publication: bool = False
):
    """Execute only the generated hydration block with local test fixtures."""
    import subprocess

    artifact_dir = tmp_path / "artifacts"
    bundle = tmp_path / "source.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    monkeypatch.setattr(
        compute_backends_module, "_GCP_ARTIFACT_DIR_PATH", str(artifact_dir)
    )
    source_id = uuid.uuid4()
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="true",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        runtime_deadline_seconds=3600,
        input_artifact_job_id=str(source_id),
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec,
        artifact_upload_url="https://storage.example.com/upload",
        input_artifact_download_url=bundle.as_uri(),
    )
    start_marker = "rm -rf /tmp/blueprint_resume_stage; rm -f /tmp/blueprint_resume_bundle.tar.gz"
    start = script.index(start_marker)
    end = script.index(start_marker, start + 1) + len(start_marker)
    block = script[start:end]
    prelude = (
        "curl() { return 1; }; upload_artifacts() { :; }; "
        "poweroff() { :; }; shutdown() { :; }; TTL_PID=; _NP_WATCHDOG_PID=; "
    )
    if fail_publication:
        prelude += "cp() { return 1; }; "
    proc = subprocess.run(
        ["bash", "-c", prelude + block],
        capture_output=True,
        text=True,
    )
    return proc, artifact_dir, source_id


def test_gcp_resume_rejects_empty_accepted_archive(monkeypatch, tmp_path):
    """An archive with no regular files cannot become a successful checkpoint."""
    proc, artifact_dir, _source_id = _run_gcp_resume_block(
        monkeypatch, tmp_path, members={}
    )

    assert proc.returncode == 1
    assert not (artifact_dir / "resume_hydration.ok").exists()
    assert (artifact_dir / "resume_hydration.failed").is_file()
    assert "resume_hydration_failed" in proc.stdout


def test_gcp_resume_rejects_archive_with_only_unsafe_member(monkeypatch, tmp_path):
    """Skipped traversal members cannot count as a hydrated checkpoint."""
    proc, artifact_dir, _source_id = _run_gcp_resume_block(
        monkeypatch,
        tmp_path,
        members={"../outside/checkpoint.pt": b"weights"},
    )

    assert proc.returncode == 1
    assert not (artifact_dir / "resume_hydration.ok").exists()
    assert (artifact_dir / "resume_hydration.failed").is_file()
    assert "resume_hydration_failed" in proc.stdout


def test_gcp_resume_scrubs_failed_publication(monkeypatch, tmp_path):
    """A failed staged copy is checked and no partial checkpoint survives."""
    proc, artifact_dir, _source_id = _run_gcp_resume_block(
        monkeypatch,
        tmp_path,
        members={"checkpoint/model.pt": b"weights"},
        fail_publication=True,
    )

    assert proc.returncode == 1
    assert not (artifact_dir / "checkpoint/model.pt").exists()
    assert not (artifact_dir / "resume_hydration.ok").exists()
    assert (artifact_dir / "resume_hydration.failed").is_file()


def test_gcp_resume_provenance_is_valid_for_chained_source(monkeypatch, tmp_path):
    """Successful GCP hydration emits every field the next chain hop requires."""
    proc, _artifact_dir, source_id = _run_gcp_resume_block(
        monkeypatch, tmp_path, members={"checkpoint/model.pt": b"weights"}
    )

    assert proc.returncode == 0, proc.stderr
    serial = "\n".join(
        [
            "BLUEPRINT_COMPUTE_STATUS=RUNNING",
            next(
                line
                for line in proc.stdout.splitlines()
                if line.startswith(compute_backends_module._SERIAL_RESUME_PREFIX)
            ),
        ]
    )
    status = compute_backends_module._parse_serial_status(serial)
    assert status is not None and status.resume_provenance is not None
    provenance = status.resume_provenance
    assert provenance["resumed_from_job_id"] == str(source_id)
    assert provenance["file_count"] == 1
    assert datetime.fromisoformat(provenance["recorded_at"]).tzinfo is not None

    resumed_gcp_job = MagicMock(
        backend=compute_backends_module.GCP_BACKEND_NAME,
        polling_metadata={
            "input_artifact_job_id": str(source_id),
            "resume_provenance": provenance,
        },
    )
    assert compute_jobs._has_verified_resume_provenance(resumed_gcp_job) is True


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
    source_id = uuid.uuid4()
    job = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend=backend.name,
        external_compute_id="compute-job-vm-fast0001",
        status=ComputeJobStatus.RUNNING,
        polling_metadata={
            "zone": "us-central1-f",
            "input_artifact_job_id": str(source_id),
        },
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
                resume_provenance={
                    "ok": True,
                    "file_count": 3,
                    "recorded_at": "2026-07-15T00:00:00+00:00",
                    "resumed_from_job_id": str(source_id),
                },
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
    assert status.resume_provenance is not None
    assert status.resume_provenance["ok"] is True
    assert status.resume_provenance["resumed_from_job_id"] == str(source_id)
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


def test_gcp_startup_script_decodes_viewable_watch_script():
    """The GCP startup script decodes the viewable_watch.py base64 blob into /tmp."""
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

    # The base64-encoded viewable_watch.py is decoded to /tmp/blueprint_viewable_watch.py
    assert "blueprint_viewable_watch.py" in script
    assert "base64 -d > /tmp/blueprint_viewable_watch.py" in script


def test_gcp_startup_script_launches_viewable_watcher_background():
    """The viewable watcher runs as a background process with the expected args."""
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

    # The watcher is launched as a background process (trailing &).
    assert "blueprint_viewable_watch.py" in script
    assert "&" in script[script.index("blueprint_viewable_watch.py"):]


def test_gcp_startup_script_kills_viewable_watcher_on_cleanup():
    """The viewable watcher PID is included in the main cleanup kill command."""
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

    # The final kill command must include _VIEWABLE_PID alongside the others.
    assert '$_VIEWABLE_PID' in script
    # The PID must be pre-declared before the setup-failure branches.
    assert '_VIEWABLE_PID=' in script


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
    # The adopted VM was provisioned by this backend with an HMAC startup script,
    # so the adopt path must stamp the same marker as the normal launch path —
    # otherwise _accepts_legacy_compute_heartbeat would accept a bare job-UUID
    # bearer and let anyone knowing the job UUID spoof its heartbeats.
    assert result.metadata.get("heartbeat_auth") == "hmac", (
        "GCP crash-recovery adopt path must stamp heartbeat_auth='hmac' so legacy "
        "UUID-bearer heartbeats are rejected for the recovered job"
    )


@pytest.mark.asyncio


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


