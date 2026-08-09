"""
Tests for runner dispatch, VM heartbeat, compute job report, and credential
redaction in app/compute.py, app/compute_backends.py, app/compute_jobs.py,
and app/schemas.py.

Covers:
- Command-log credential redaction (bearer tokens, signed URLs, shell exports).
- VM heartbeat authentication (HMAC vs UUID, rollout date, bearer rejection).
- Provisioning deadline and launch-LRO timeout interactions with heartbeat.
- next_compute_job claim protocol and project/runner scoping.
- compute_job_report stamping, finalization, and idempotency.
- try_runner_dispatch GPU/CPU routing and dead-runner fallback.
- materialize_local_manifest path-traversal guard.
- retry_provisioning_timeout backoff and exhaustion.
- spawn_followup operator/engine authored task handling.
- Progress sample ring buffer, series bridging, and watchdog protection status.
- serialize_compute_job_response_safe promotion of progress series.
- runner_bridge last_progress_at from written samples.
- compute_experiment container image default.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.time_utils import _utcnow
from app import compute
from app import compute_backends as compute_backends_module
from app import compute_jobs
from app import schemas
from app.compute_backends import (
    ComputeLaunchResult,
)
from app.database import resolve_async_engine
from app.models import (
    ComputeJob,
    ComputeJobOutcome,
    ComputeJobStatus,
    Project,
    Task,
    TaskStatus,
    WakeAction,
)

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
    # Stamp the HMAC launch marker: this job was launched by the HMAC engine so
    # its startup script contains the per-job secret, not the bare UUID.
    job.polling_metadata = {"heartbeat_auth": "hmac"}
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
async def test_compute_job_vm_heartbeat_pre_rollout_uuid_keeps_vm_alive(
    client, db_session, monkeypatch
):
    """A VM launched before the HMAC deploy may refresh its heartbeat with its UUID."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="HB Flag-day Compatibility", user_id="u")
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
    job.status = ComputeJobStatus.RUNNING
    # No heartbeat_auth marker: simulates a row created by the pre-HMAC engine
    # whose startup script sends the bare job UUID as the heartbeat bearer.
    job.polling_metadata = {}
    job.last_heartbeat_at = None
    await db_session.commit()

    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/heartbeat",
        headers={"Authorization": f"Bearer {job.id}"},
    )

    assert resp.status_code == 204
    await db_session.refresh(job)
    assert job.last_heartbeat_at is not None


@pytest.mark.asyncio
async def test_compute_job_vm_heartbeat_unmarked_job_after_rollout_date_accepts_uuid(
    client, db_session, monkeypatch
):
    """An unmarked job (no heartbeat_auth marker) launched AFTER the old rollout date
    must still accept a legacy UUID bearer.

    This is the exact case the v26 qualification finding cites: VMs launched by
    the still-serving pre-HMAC engine have no marker in polling_metadata regardless
    of their created_at timestamp.  The marker-based check replaces the old
    calendar-keyed check, so post-rollout-date rows without the marker are treated
    as pre-HMAC (legacy UUID bearer accepted).
    """
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="HB Marker Unmarked Post-Rollout", user_id="u")
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
    job.status = ComputeJobStatus.RUNNING
    # No heartbeat_auth marker and created_at AFTER the old rollout boundary.
    # Under the old calendar-keyed scheme this would be rejected; under the new
    # marker-based scheme it must be accepted (no marker = legacy engine).
    job.polling_metadata = {}
    job.created_at = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc).replace(
        tzinfo=None
    )
    job.last_heartbeat_at = None
    await db_session.commit()

    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/heartbeat",
        headers={"Authorization": f"Bearer {job.id}"},
    )
    assert resp.status_code == 204, (
        "unmarked job (no heartbeat_auth marker) must accept legacy UUID bearer "
        "regardless of created_at — this is the exact v26 gating bug"
    )
    await db_session.refresh(job)
    assert job.last_heartbeat_at is not None, "heartbeat must update liveness"


@pytest.mark.asyncio
async def test_compute_job_vm_heartbeat_marked_job_rejects_uuid_accepts_hmac(
    client, db_session, monkeypatch
):
    """A job with heartbeat_auth='hmac' marker rejects UUID bearer and accepts HMAC."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="HB Marker HMAC Only", user_id="u")
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
    job.polling_metadata = {"heartbeat_auth": "hmac"}
    await db_session.commit()

    # UUID bearer must be rejected.
    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/heartbeat",
        headers={"Authorization": f"Bearer {job.id}"},
    )
    assert resp.status_code == 403, "marked job must reject bare UUID bearer"

    # HMAC bearer must be accepted.
    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/heartbeat",
        headers={
            "Authorization": (
                f"Bearer {compute_backends_module.compute_job_heartbeat_secret(job.id)}"
            )
        },
    )
    assert resp.status_code == 204, "marked job must accept HMAC bearer"


@pytest.mark.asyncio
async def test_compute_job_vm_heartbeat_malformed_bearer_no_500(
    client, db_session, monkeypatch
):
    """A non-ASCII or empty bearer must never cause a 500 — always 403."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="HB Malformed Bearer", user_id="u")
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
    job.polling_metadata = {"heartbeat_auth": "hmac"}
    await db_session.commit()

    for bad_bearer in ["", "not-a-uuid", "x" * 256, "null"]:
        resp = await client.post(
            f"/api/agent/compute-jobs/{job.id}/heartbeat",
            headers={"Authorization": f"Bearer {bad_bearer}"},
        )
        assert resp.status_code in (403, 422), (
            f"malformed bearer {bad_bearer!r} must not 500"
        )


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
            # Simulate a slow GCP insert LRO that outlasts the provisioning
            # budget by blocking until wait_for cancels it at the deadline.
            await asyncio.Event().wait()
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
    assert meta.get("runner_command_log") == {
        "text": "Training complete",
        "truncated": False,
        "source": "runner",
    }
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
    keys stores only {text, truncated, source} in polling_metadata."""
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
    assert stored_log == {"text": "Done", "truncated": False, "source": "runner"}
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
async def test_admin_ui_task_dispatch_requires_viewport_capable_runner(db_session):
    """Admin-UI source changes use the same viewport prerequisite as ``check``.

    The task's declared ``related_files`` is the pre-PR dispatch scope; a later
    ``_pr_changed_files`` context is covered by the same production predicate.
    An unknown/partial runner inventory must not claim the task.
    """
    from app import auth as auth_mod
    from app import orchestrator as orch
    from app.models import LocalRunner

    project = Project(name="Viewport Route", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    def _make_runner(inventory):
        token = auth_mod.create_runner_token(uuid.uuid4())
        return LocalRunner(
            project_id=project.id,
            token_hash=auth_mod.hash_runner_token(token),
            capabilities={
                "task_types": "all",
                "agent_available": True,
                "isolation_backends": inventory,
            },
        )

    incapable = _make_runner({"docker_usable": True})
    capable = _make_runner(
        {
            "docker_usable": True,
            "playwright_chromium_usable": True,
        }
    )
    db_session.add_all([incapable, capable])
    await db_session.commit()
    await db_session.refresh(incapable)
    await db_session.refresh(capable)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "refactor"}],
        context={"related_files": ["admin-ui/src/components/Widget.tsx"]},
        status=TaskStatus.QUEUED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    assert (
        await orch._try_runner_dispatch(
            db_session, task, {project.id: [incapable]}
        )
        is False
    )
    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED

    assert (
        await orch._try_runner_dispatch(
            db_session, task, {project.id: [incapable, capable]}
        )
        is True
    )
    await db_session.refresh(task)
    assert task.status == TaskStatus.PROVISIONING
    assert task.vm_instance_id == f"runner:{capable.id}"


@pytest.mark.asyncio
async def test_viewport_block_retries_when_capable_runner_appears(monkeypatch):
    """A formerly permanent viewport block re-arms when the fleet can satisfy it."""
    from app import orchestrator_runner_dispatch as dispatch
    from app.blocked_task_unblock_sweeps import _blocked_precondition_state
    from app.runner_eligibility import RunnerDispatchSnapshot

    project_id = uuid.uuid4()
    capable = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities={
            "isolation_backends": {
                "docker_usable": True,
                "playwright_chromium_usable": True,
            }
        },
    )

    async def _active_runners(_session):
        return {project_id: [capable]}

    monkeypatch.setattr(dispatch, "_load_active_runners_cache", _active_runners)
    task = SimpleNamespace(
        project_id=project_id,
        depends_on=[],
        blocker_kind=None,
        blocked_reason="BLOCKER: VIEWPORT_GATE_INFRA_UNAVAILABLE: no Docker",
        latest_status_message="viewport bootstrap unavailable",
        first_payload={"type": "refactor"},
        context={
            "blocker": {"source": "agent"},
            "related_files": ["admin-ui/src/components/Widget.tsx"],
        },
    )

    assert await _blocked_precondition_state(task, object(), lambda: None) == "retry"


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


def test_materialize_local_manifest_invalid_mode_does_not_crash():
    """_materialize_local_manifest tolerates a non-octal mode string without raising.

    int(..., 8) raises ValueError on non-octal strings (e.g. "invalid", "0x1ff").
    The catch must include ValueError so a bad mode degrades to a warning rather than
    aborting the whole manifest write — the file was already written successfully.
    """
    import tempfile
    from pathlib import Path

    env = compute._materialize_local_manifest(
        "test-vm-bad-mode",
        {"files": [{"path": "/tmp/badmode.txt", "content": "ok", "mode": "not-octal"}]},
    )
    expected_root = Path(tempfile.gettempdir()) / "blueprint-agent-state" / "test-vm-bad-mode"
    expected_file = expected_root / "tmp" / "badmode.txt"
    assert expected_file.read_text(encoding="utf-8") == "ok", (
        "file must be written even when mode is invalid"
    )
    assert env == {}, "no env vars expected for this manifest"


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
async def test_spawn_followup_is_suppressed_without_opt_in_for_engine_task(db_session, monkeypatch):
    """Engine-authored tasks also require explicit spawn_followup=True."""
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
    assert task.status == TaskStatus.COMPLETED
    spawn_mock.assert_not_called()


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


# ── Progress-series compaction + terminal_kind disposition ──────────────────

def test_append_progress_sample_basic():
    """A valid metric+value is appended as {t, name, value} into progress_series."""
    from app.compute_jobs import append_progress_sample

    meta = {}
    result = append_progress_sample(meta, metric="loss", value=0.42, t="2026-01-01T00:00:00")
    series = result["progress_series"]
    assert len(series) == 1
    assert series[0]["name"] == "loss"
    assert series[0]["value"] == 0.42
    assert datetime.fromisoformat(series[0]["received_at"]) <= _utcnow()
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
    """The per-metric history retains the last 1,500 minute buckets."""
    from app.compute_jobs import append_progress_sample

    meta: dict = {}
    start = datetime(2026, 1, 1)
    for i in range(1505):
        meta = append_progress_sample(
            meta,
            metric="step",
            value=float(i),
            t=(start + timedelta(minutes=i)).isoformat(),
        )
    series = meta["progress_series"]
    assert len(series) == 1500
    assert series[0]["value"] == 5.0, "oldest 5 samples should have been dropped"
    assert series[-1]["value"] == 1504.0


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
            {"metric": "reward", "value": 2.0, "ts": "2026-01-01T00:01:01"},
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
            received_at=now,
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
    """serialize_compute_job_response_safe promotes watchdog-protection status
    fields as first-class fields on the operator-facing payload.

    Three sub-cases:
    (1) Protected running job: declared metric + enough fresh receipt-time history
        -> watchdog_active=True and watchdog_state='protected'.
    (2) No declared metric: watchdog_active=False, watchdog_inert_reason='no_declared_metric'.
    (3) Metric-name mismatch: samples exist but none match declared metric ->
        watchdog_inert_reason='metric_name_mismatch'.
    """
    from app.schemas import serialize_compute_job_response_safe
    from app.models import _utcnow

    # --- Case 1: protected job with declared metric and matching samples ---
    now = _utcnow()
    progress_series = [
        {
            "t": "runner-authored",
            "received_at": (now - timedelta(seconds=age)).isoformat(),
            "name": "reward",
            "value": 1.0,
        }
        for age in (3600, 2700, 1800, 900, 0)
    ]
    job_protected = ComputeJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        backend="local-subprocess",
        status=ComputeJobStatus.RUNNING,
        spend_cap_usd=1.0,
        started_at=now - timedelta(hours=2),
        polling_metadata={
            "declared_metric": "reward",
            "no_progress_timeout_seconds": 3600,
            "progress_series": progress_series,
        },
    )
    data = serialize_compute_job_response_safe(job_protected)
    assert data["watchdog_active"] is True, "watchdog must be active for a protected job"
    assert data["watchdog_state"] == "protected"
    assert data["no_progress_timeout_seconds"] == 3600, "per-job timeout must be reported"
    assert data["last_progress_at"] == now.isoformat(), "must return newest server receipt time"
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
                {
                    "t": "runner-authored",
                    "received_at": now.isoformat(),
                    "name": "loss",
                    "value": 0.5,
                },
            ],
        },
    )
    data3 = serialize_compute_job_response_safe(job_mismatch)
    assert data3["watchdog_active"] is False
    assert data3["watchdog_state"] == "inert"
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
            received_at=now,
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

    # The bridge preserves runner-authored t for presentation but the watchdog
    # exposes authenticated server receipt time as last_progress_at.
    assert data["progress_series"][-1]["t"] == "2026-06-01T00:01:00"
    assert data["last_progress_at"] == now.isoformat(), (
        "last_progress_at must use server receipt time, not the runner timestamp"
    )
    assert data["watchdog_active"] is False
    assert data["watchdog_state"] == "insufficient_history"
    assert data["watchdog_inert_reason"] == "missing_start_time"


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
                "compute": {
                    "image": {
                        "name": _PROJECT_IMAGE,
                        "build": {"dockerfile": "Dockerfile"},
                    }
                },
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
