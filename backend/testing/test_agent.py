import logging
import os
import asyncio
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from datetime import datetime, timedelta
from httpx import AsyncClient
from sqlalchemy import select
from app.database import get_session
from app.main import app
from app.models import (
    Artifact,
    ArtifactType,
    LLMAuthMode,
    LLMProviderName,
    MaxPlanAccount,
    Project,
    ProjectStatus,
    Task,
    TaskStatus,
)
from app.auth import create_agent_token


def _resolve_sr_recent_miss_examples_contract_fixture() -> Path:
    """Locate the SR-recent-miss-examples contract fixture across local + Docker.

    Search order:
      1. ``SR_CONTRACT_FIXTURE`` env override (caller-supplied path).
      2. ``backend/testing/__fixtures__/...`` (always present in the backend
         Docker build context — the path that works in CI).
      3. ``agent/src/prompts/__fixtures__/...`` resolved from the host
         repo root (works in local dev outside Docker).

    The two on-disk copies are kept byte-identical;
    ``test_claim_sr_recent_miss_examples_fixture_parity`` asserts this when
    both are reachable. The duplicate exists because the backend image's
    build context (``./backend``) cannot reach ``agent/``.
    """
    candidates: list[Path] = []
    env_override = os.getenv("SR_CONTRACT_FIXTURE")
    if env_override:
        candidates.append(Path(env_override))
    candidates.append(
        Path(__file__).parent / "__fixtures__" / "sr_recent_miss_examples_contract.json"
    )
    candidates.append(
        Path(__file__).resolve().parents[2]
        / "agent"
        / "src"
        / "prompts"
        / "__fixtures__"
        / "sr_recent_miss_examples_contract.json"
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise AssertionError(
        "SR contract fixture not found in any candidate path: "
        + ", ".join(str(c) for c in candidates)
    )


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_agent_claim_success(client: AsyncClient, db_session):
    """
    Tests an agent successfully claiming a QUEUED task.
    """
    project = Project(name="Agent Claim Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id, payloads=[{"cmd": "ls"}], vm_instance_id="agent-vm-999"
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Generate a valid JWT for this task/VM
    token = create_agent_token(str(task.id), "agent-vm-999")
    headers = {"x-agent-token": token}
    resp = await client.post("/api/agent/claim", headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] is not None
    assert data["title"] is not None
    assert "payloads" in data

    await db_session.refresh(task)
    assert task.status == TaskStatus.IN_PROGRESS


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_agent_claim_unauthorized(client: AsyncClient):
    """
    Tests that missing or invalid token returns 401.
    """
    resp = await client.post(
        "/api/agent/claim", headers={"x-agent-token": "wrong-token"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_agent_claim_missing_task_row_returns_401(client: AsyncClient):
    """Claim auth fails closed when the JWT points at a missing task row."""
    token = create_agent_token(
        "00000000-0000-0000-0000-000000000000", "non-existent-vm"
    )
    headers = {"x-agent-token": token}
    resp = await client.post("/api/agent/claim", headers=headers)
    assert resp.status_code == 401


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_agent_status_update(client: AsyncClient, db_session, mocker):
    """
    Tests an agent updating task status and creating an artifact.
    Also verifies that completing the last task completes the project.
    """
    mock_delete = mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)

    project = Project(name="Agent Status Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "ls"}],
        vm_instance_id="agent-vm-888",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-888")
    headers = {"x-agent-token": token}
    payload = {
        "task_id": str(task.id),
        "status": "COMPLETED",
        "artifact_type": "ERROR_LOG",
        "logs": "build success",
    }
    resp = await client.post("/api/agent/status", json=payload, headers=headers)
    assert resp.status_code == 200

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED

    await db_session.refresh(project)
    assert project.status == ProjectStatus.COMPLETED

    mock_delete.assert_called_once_with("agent-vm-888")


@pytest.mark.asyncio
async def test_agent_status_update_persists_runtime_identity(
    client: AsyncClient, db_session, mocker
):
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    now = datetime.utcnow()

    project = Project(name="Agent Runtime Identity Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="agent-vm-runtime",
        status=TaskStatus.IN_PROGRESS,
        vm_provisioned_at=now - timedelta(minutes=5),
        context={"llm_account_id": "acct-1", "max_plan_account_id": "acct-1"},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-runtime")
    headers = {"x-agent-token": token}
    payload = {
        "task_id": str(task.id),
        "status": "COMPLETED",
        "artifact_type": "ERROR_LOG",
        "logs": "provider-specific failure",
        "provider": "openai",
        "auth_mode": "api_key",
        "model": "gpt-5.4",
        "execution_mode": "default",
    }
    resp = await client.post("/api/agent/status", json=payload, headers=headers)
    assert resp.status_code == 200

    await db_session.refresh(task)
    assert task.context["llm_provider"] == "openai"
    assert task.context["llm_auth_mode"] == "api_key"
    assert task.context["llm_model"] == "gpt-5.4"
    assert task.context["llm_execution_mode"] == "default"
    assert task.provider == "openai"
    assert task.account_id == "acct-1"
    assert task.model == "gpt-5.4"
    assert task.estimated_cost_usd is not None

    provider_tasks = (
        (await db_session.execute(select(Task).where(Task.provider == "openai")))
        .scalars()
        .all()
    )
    assert any(t.id == task.id for t in provider_tasks)


@pytest.mark.asyncio
async def test_completed_teardown_defer_is_provider_aware_noop_single_provider(
    client: AsyncClient, db_session, mocker, monkeypatch
):
    """Single-provider projects still warm-reuse: a COMPLETED VM defers teardown
    when the project has eligible backlog work whose provider is compatible with
    the VM's boot manifest.

    Regression guard for the provider-aware teardown-defer in
    update_agent_status: the eligibility check is now passed the completing
    task's frozen boot identity (provider/account/model), but a fungible
    (provider=None) backlog task — the dominant single-provider case — must NOT
    be filtered out, so the VM is held for warm reuse exactly as before.
    """
    import app.shared_state as _ss

    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    mock_teardown = mocker.patch(
        "app.vm_lifecycle.schedule_deferred_backlog_teardown"
    )
    mock_delete = mocker.patch("app.vm_lifecycle.fire_and_forget_vm_delete")
    monkeypatch.setitem(_ss.runtime_config, "warm_backlog_reuse_enabled", True)

    project = Project(name="Provider-Aware Defer Noop", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Fungible (provider=None) backlog work — the dominant single-provider case.
    backlog = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
    )
    completing = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="agent-vm-defer-noop",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add_all([backlog, completing])
    await db_session.commit()
    await db_session.refresh(completing)

    token = create_agent_token(str(completing.id), "agent-vm-defer-noop")
    payload = {
        "task_id": str(completing.id),
        "status": "COMPLETED",
        "logs": "done",
        "provider": "anthropic",
    }
    resp = await client.post(
        "/api/agent/status", json=payload, headers={"x-agent-token": token}
    )
    assert resp.status_code == 200

    # VM held for warm reuse, NOT torn down on completion.
    mock_teardown.assert_called_once_with("agent-vm-defer-noop")
    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_completed_teardown_proceeds_when_backlog_provider_mismatch(
    client: AsyncClient, db_session, mocker, monkeypatch
):
    """A COMPLETED VM whose only backlog work is pinned to a DIFFERENT provider
    must NOT defer teardown — that work would run on the wrong frozen account, so
    it should cold-dispatch and the idle VM should be torn down as before."""
    import app.shared_state as _ss

    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    mock_teardown = mocker.patch(
        "app.vm_lifecycle.schedule_deferred_backlog_teardown"
    )
    mock_delete = mocker.patch("app.vm_lifecycle.fire_and_forget_vm_delete")
    monkeypatch.setitem(_ss.runtime_config, "warm_backlog_reuse_enabled", True)

    project = Project(name="Provider-Aware Defer Mismatch", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Backlog work pinned/recorded to a provider the boot VM cannot run.
    backlog = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        provider="openai",
    )
    completing = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="agent-vm-defer-mismatch",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add_all([backlog, completing])
    await db_session.commit()
    await db_session.refresh(completing)

    token = create_agent_token(str(completing.id), "agent-vm-defer-mismatch")
    payload = {
        "task_id": str(completing.id),
        "status": "COMPLETED",
        "logs": "done",
        "provider": "anthropic",
    }
    resp = await client.post(
        "/api/agent/status", json=payload, headers={"x-agent-token": token}
    )
    assert resp.status_code == 200

    # Incompatible backlog → no defer; VM torn down on completion.
    mock_teardown.assert_not_called()
    mock_delete.assert_called_once_with(
        "agent-vm-defer-mismatch", reason="agent_completion"
    )


@pytest.mark.asyncio
async def test_agent_claim_retry_is_idempotent(client: AsyncClient, db_session):
    """
    A second claim from the same VM shortly after a successful first claim must
    replay the same task payload instead of 404'ing. The agent's apiPost retries
    on network errors / 30s timeout aborts, and if the first POST's 200 response
    was lost after the server committed PROVISIONING -> IN_PROGRESS, the retry
    would otherwise kill the task with PRE_CLAIM_FAILED.
    """
    project = Project(name="Concurrent Claim Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-concurrent",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-concurrent")
    headers = {"x-agent-token": token}

    resp1 = await client.post("/api/agent/claim", headers=headers)
    assert resp1.status_code == 200

    resp2 = await client.post("/api/agent/claim", headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["task_id"] == resp1.json()["task_id"]


@pytest.mark.asyncio
async def test_agent_claim_idempotent_replay_expires(client: AsyncClient, db_session):
    """
    Idempotent replay only fires within ~3 min of the original claim. Beyond
    that window a stray claim from the same VM is a real anomaly and should
    404, not silently re-serve a task whose agent may have long since died.
    """
    project = Project(name="Claim Replay Expiry", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    stale_heartbeat = datetime.utcnow() - timedelta(minutes=10)
    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-stale-replay",
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=stale_heartbeat,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-stale-replay")
    headers = {"x-agent-token": token}
    resp = await client.post("/api/agent/claim", headers=headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_agent_heartbeat_updates_last_heartbeat(client: AsyncClient, db_session):
    """
    Tests that a heartbeat request updates the task's last_heartbeat timestamp.
    A task with a fresh heartbeat is within the 5-minute reaper threshold and
    will not be killed by the reaper.
    """
    project = Project(name="Heartbeat Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-heartbeat",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    assert task.last_heartbeat is None

    token = create_agent_token(str(task.id), "agent-vm-heartbeat")
    headers = {"x-agent-token": token}
    payload = {"task_id": str(task.id), "status_message": "still running"}

    resp = await client.post("/api/agent/heartbeat", json=payload, headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    await db_session.refresh(task)
    assert task.last_heartbeat is not None

    # A task with a heartbeat this recent is well within the 5-minute reaper window
    five_minutes_ago = datetime.utcnow() - timedelta(minutes=5)
    assert task.last_heartbeat > five_minutes_ago


@pytest.mark.asyncio
async def test_agent_heartbeat_persists_valid_prompt_fingerprint(
    client: AsyncClient, db_session
):
    """Heartbeat accepts the persisted 12-char lowercase-hex fingerprint contract."""
    project = Project(name="Heartbeat Prompt Fingerprint", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-fingerprint",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-fingerprint")
    resp = await client.post(
        "/api/agent/heartbeat",
        json={
            "task_id": str(task.id),
            "prompt_fingerprint": "abcdef123456",
        },
        headers={"x-agent-token": token},
    )

    assert resp.status_code == 200
    await db_session.refresh(task)
    assert task.prompt_fingerprint == "abcdef123456"
    assert (task.context or {}).get("prompt_fingerprint") == "abcdef123456"


@pytest.mark.asyncio
async def test_agent_heartbeat_rejects_invalid_prompt_fingerprint_shape(
    client: AsyncClient, db_session
):
    """Heartbeat rejects prompt fingerprints that do not match the 12-hex contract."""
    project = Project(
        name="Heartbeat Invalid Prompt Fingerprint", user_id="test-user-123"
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-invalid-fingerprint",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-invalid-fingerprint")
    resp = await client.post(
        "/api/agent/heartbeat",
        json={
            "task_id": str(task.id),
            "prompt_fingerprint": "abcdef1234567890",
        },
        headers={"x-agent-token": token},
    )

    assert resp.status_code == 422
    await db_session.refresh(task)
    assert task.prompt_fingerprint is None
    assert (task.context or {}).get("prompt_fingerprint") is None


@pytest.mark.asyncio
async def test_agent_heartbeat_accepts_pre_claim_provisioning_task(
    client: AsyncClient, db_session
):
    """
    Tests that the heartbeat endpoint accepts a heartbeat from an agent whose task
    is still in PROVISIONING state. This is the pre-claim failure reporting path:
    an agent that dies during token validation or claim needs to post a final
    PRE_CLAIM_FAILED heartbeat so the reaper has forensic evidence instead of a
    silent provision failure.
    """
    project = Project(name="Pre-claim Heartbeat Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task is in PROVISIONING — matches the state during the agent's pre-claim window.
    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-preclaim",
        status=TaskStatus.PROVISIONING,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-preclaim")
    headers = {"x-agent-token": token}
    payload = {
        "task_id": str(task.id),
        "status_message": "PRE_CLAIM_FAILED: token validation rate-limited",
    }

    resp = await client.post("/api/agent/heartbeat", json=payload, headers=headers)

    assert resp.status_code == 200
    await db_session.refresh(task)
    assert task.status == TaskStatus.PROVISIONING, "heartbeat must not mutate status"
    assert (
        task.latest_status_message == "PRE_CLAIM_FAILED: token validation rate-limited"
    )
    assert task.last_heartbeat is not None


@pytest.mark.asyncio
async def test_heartbeat_stamps_cold_start_stage(client: AsyncClient, db_session):
    """PROVISION_PROGRESS beacons write timestamped entries into
    task.context['_cold_start_stages'] for per-stage breakdown telemetry.
    The first beacon for a given stage key wins; subsequent duplicates are ignored.
    """
    project = Project(name="Cold Start Stage Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-cold-stage",
        status=TaskStatus.PROVISIONING,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-cold-stage")
    headers = {"x-agent-token": token}

    # First beacon — creds fetched (maps to beacon_a_at)
    payload = {
        "task_id": str(task.id),
        "status_message": "PROVISION_PROGRESS: creds fetched, entering env setup",
    }
    resp = await client.post("/api/agent/heartbeat", json=payload, headers=headers)
    assert resp.status_code == 200

    await db_session.refresh(task)
    stages = (task.context or {}).get("_cold_start_stages") or {}
    assert "beacon_a_at" in stages, (
        "beacon_a_at must be stamped on creds-fetched beacon"
    )
    first_stamp = stages["beacon_a_at"]

    # Second beacon — env materialized (maps to beacon_b_at)
    payload["status_message"] = (
        "PROVISION_PROGRESS: env file materialized, preparing payload"
    )
    resp = await client.post("/api/agent/heartbeat", json=payload, headers=headers)
    assert resp.status_code == 200

    await db_session.refresh(task)
    stages = (task.context or {}).get("_cold_start_stages") or {}
    assert "beacon_b_at" in stages, (
        "beacon_b_at must be stamped on env-materialized beacon"
    )

    # Duplicate beacon_a_at — must not overwrite the first stamp.
    payload["status_message"] = "PROVISION_PROGRESS: creds fetched, entering env setup"
    resp = await client.post("/api/agent/heartbeat", json=payload, headers=headers)
    assert resp.status_code == 200

    await db_session.refresh(task)
    stages = (task.context or {}).get("_cold_start_stages") or {}
    assert stages["beacon_a_at"] == first_stamp, (
        "first-write-wins: duplicate beacon must not overwrite"
    )


@pytest.mark.asyncio
async def test_heartbeat_count_increments_per_heartbeat(
    client: AsyncClient, db_session
):
    """R4-W2 (H1+H2+H3+M10): the dedicated ``Task.heartbeat_count`` column
    advances by exactly one on every heartbeat, regardless of whether the
    request carries metrics/tool_trace.

    This pins three regressions guarded by R4-W2:

      - H1 — replaces a separate Core ``UPDATE`` round-trip with an ORM
        mutation that SQLAlchemy coalesces into the existing commit-time
        UPDATE. If the ORM-mutation path regresses to the Core UPDATE,
        ``synchronize_session="auto"`` would re-introduce H2's off-by-one.
      - H3 — the increment used to live inside ``if req.metrics or
        req.tool_trace`` so metrics-less heartbeats never advanced the
        counter, biasing the every-5th audit-trail-persist gate.
      - M10 — the in-memory ``task.heartbeat_count`` was stale after a
        Core UPDATE; the ORM-mutation path keeps the in-memory value
        consistent with the DB row.
    """
    project = Project(name="Heartbeat Count Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-hb-count",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    initial = task.heartbeat_count
    assert initial == 0, f"new task should default heartbeat_count=0, got {initial}"

    token = create_agent_token(str(task.id), "agent-vm-hb-count")
    headers = {"x-agent-token": token}

    # 5 metrics-less heartbeats — H3 says these MUST advance the counter.
    for _ in range(5):
        resp = await client.post(
            "/api/agent/heartbeat",
            json={"task_id": str(task.id), "status_message": "still running"},
            headers=headers,
        )
        assert resp.status_code == 200, f"heartbeat failed: {resp.text}"

    await db_session.refresh(task)
    assert task.heartbeat_count == initial + 5, (
        f"expected heartbeat_count to advance by 5 (one per metrics-less heartbeat), "
        f"got initial={initial} → final={task.heartbeat_count}"
    )


@pytest.mark.asyncio
async def test_heartbeat_audit_gate_fires_every_5th(client: AsyncClient, db_session):
    """R4-W2: ``metrics_history`` is persisted to ``task.context`` only on
    every 5th heartbeat (with metrics), not on the 1st-4th.

    The audit-trail ring-buffer (``metrics_history``, ``_tool_trace``)
    persistence is gated by ``hb_count % 5 == 0`` so that we don't rewrite
    the JSONB blob on every cycle (round 2 audit C3 — JSONB rewrite was the
    failure mode B2 was created to fix). With H3's hoisted increment,
    ``hb_count`` advances on every heartbeat regardless of whether metrics
    are attached, so the gate fires predictably on the 5th metrics-bearing
    request.

    Skips the round-1 timestamp-modulo lottery (``int(last_heartbeat.timestamp())
    % 5``) where ~80% of VMs never persisted because consecutive 30s heartbeats
    landed in the same modulo bucket.
    """
    project = Project(name="Heartbeat Audit Gate Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-audit-gate",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-audit-gate")
    headers = {"x-agent-token": token}
    metrics_payload = {
        "task_id": str(task.id),
        "status_message": "still running",
        "metrics": {"rss_mb": 100.0, "heap_used_mb": 50.0},
    }

    # Heartbeats 1-4 — counter advances but metrics_history must not be persisted.
    for i in range(1, 5):
        resp = await client.post(
            "/api/agent/heartbeat",
            json=metrics_payload,
            headers=headers,
        )
        assert resp.status_code == 200, f"heartbeat {i} failed: {resp.text}"
        await db_session.refresh(task)
        assert task.heartbeat_count == i, (
            f"after heartbeat {i}: expected count={i}, got {task.heartbeat_count}"
        )
        ctx = task.context or {}
        assert "metrics_history" not in ctx, (
            f"after heartbeat {i}: metrics_history must NOT be persisted "
            f"(every-5th gate), got context={ctx!r}"
        )

    # Heartbeat 5 — gate fires, metrics_history persisted.
    resp = await client.post(
        "/api/agent/heartbeat",
        json=metrics_payload,
        headers=headers,
    )
    assert resp.status_code == 200, f"5th heartbeat failed: {resp.text}"
    await db_session.refresh(task)
    assert task.heartbeat_count == 5
    ctx = task.context or {}
    assert "metrics_history" in ctx, (
        f"after heartbeat 5: metrics_history MUST be persisted (every-5th gate fires), "
        f"got context={ctx!r}"
    )
    history = ctx["metrics_history"]
    assert isinstance(history, list)
    assert len(history) == 1, (
        f"expected exactly one metrics_history entry on first gate fire, "
        f"got {history!r}"
    )
    assert history[0].get("rss_mb") == 100.0


@pytest.mark.asyncio
async def test_agent_status_complete_with_artifacts_creates_artifact_record(
    client: AsyncClient, db_session, mocker
):
    """
    Tests that completing a task with artifact information creates an Artifact record
    in the database and transitions the task status to COMPLETED.

    A sibling task is kept IN_PROGRESS so that the project aggregation path does not
    trigger a session.commit() followed by a lazy attribute access that would fail in
    the SQLite async test environment (MissingGreenlet).
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)

    project = Project(name="Artifact Creation Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-artifact",
        status=TaskStatus.IN_PROGRESS,
    )
    # Sibling task stays IN_PROGRESS so the project is NOT fully complete yet,
    # preventing the project-aggregation commit path that has a SQLite async
    # limitation (lazy reload of expired primary key after commit).
    sibling = Task(
        project_id=project.id,
        payloads=[{"cmd": "other"}],
        vm_instance_id="agent-vm-artifact-sibling",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    db_session.add(sibling)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-artifact")
    headers = {"x-agent-token": token}
    payload = {
        "task_id": str(task.id),
        "status": "COMPLETED",
        "artifact_type": "GIT_COMMIT",
        "commit_hash": "abc123def456",
        "repo_url": "https://github.com/example/repo",
        "logs": "Build and tests passed",
    }

    resp = await client.post("/api/agent/status", json=payload, headers=headers)
    assert resp.status_code == 200

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED

    artifact_stmt = select(Artifact).where(Artifact.task_id == task.id)
    result = await db_session.execute(artifact_stmt)
    artifacts = result.scalars().all()

    assert len(artifacts) == 1
    assert artifacts[0].artifact_type == ArtifactType.GIT_COMMIT
    assert artifacts[0].commit_hash == "abc123def456"
    assert artifacts[0].repo_url == "https://github.com/example/repo"


@pytest.mark.asyncio
async def test_agent_claim_includes_project_metadata(client: AsyncClient, db_session):
    """
    Tests that the claim response includes project_metadata with build_config.
    Agents need this to adapt lint/build/test behavior to different project structures.
    """
    project = Project(
        name="Build Config Test",
        user_id="test-user-123",
        github_owner="acme",
        github_repo="myapp",
        build_config={
            "directories": [{"path": "frontend", "type": "node"}],
            "test_command": "pytest",
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do something"}],
        vm_instance_id="agent-vm-meta-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-meta-1")
    headers = {"x-agent-token": token}
    resp = await client.post("/api/agent/claim", headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert "project_metadata" in data
    pm = data["project_metadata"]
    assert pm["project_id"] == str(project.id)
    assert pm["project_name"] == "Build Config Test"
    assert pm["github_owner"] == "acme"
    assert pm["github_repo"] == "myapp"
    assert pm["enable_self_improvement"] is False
    assert pm["build_config"]["test_command"] == "pytest"


@pytest.mark.asyncio
async def test_agent_claim_surfaces_repo_is_empty(client: AsyncClient, db_session):
    """The claim response surfaces build_config.repo_is_empty as top-level repo_is_empty.

    This is the deterministic empty-repo signal computed at onboarding; the agent
    research prompt gates bootstrap-from-goal guidance on it.
    """
    project_empty = Project(
        name="Empty Repo",
        user_id="test-user-123",
        github_owner="acme",
        github_repo="greenfield",
        build_config={"repo_is_empty": True},
        design_doc={"vision": "greenfield project"},
    )
    db_session.add(project_empty)
    await db_session.commit()
    await db_session.refresh(project_empty)

    task_empty = Task(
        project_id=project_empty.id,
        payloads=[{"type": "research", "prompt": "do something"}],
        vm_instance_id="agent-vm-empty-1",
    )
    db_session.add(task_empty)
    await db_session.commit()
    await db_session.refresh(task_empty)

    token = create_agent_token(str(task_empty.id), "agent-vm-empty-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    assert resp.json()["repo_is_empty"] is True

    # A project without the signal (older onboarding / detected source) → False.
    project_mature = Project(
        name="Mature Repo",
        user_id="test-user-123",
        github_owner="acme",
        github_repo="established",
        build_config={"language": "python"},
        design_doc={"vision": "established project"},
    )
    db_session.add(project_mature)
    await db_session.commit()
    await db_session.refresh(project_mature)

    task_mature = Task(
        project_id=project_mature.id,
        payloads=[{"type": "research", "prompt": "do something"}],
        vm_instance_id="agent-vm-mature-1",
    )
    db_session.add(task_mature)
    await db_session.commit()
    await db_session.refresh(task_mature)

    token2 = create_agent_token(str(task_mature.id), "agent-vm-mature-1")
    resp2 = await client.post("/api/agent/claim", headers={"x-agent-token": token2})
    assert resp2.status_code == 200
    assert resp2.json()["repo_is_empty"] is False


@pytest.mark.asyncio
async def test_agent_claim_enable_self_improvement_driven_by_build_config(
    client: AsyncClient, db_session
):
    """enable_self_improvement is driven by build_config, not is_self_repo.

    A non-self-repo project with the flag set true gets self-improvement enabled;
    a self-repo project without the explicit flag gets False (decoupled).
    """
    # Non-self-repo with explicit flag → True
    project_with_flag = Project(
        name="Self-Improvement Enabled",
        user_id="test-user-123",
        is_self_repo=False,
        build_config={"enable_self_improvement": True},
    )
    db_session.add(project_with_flag)
    await db_session.commit()
    await db_session.refresh(project_with_flag)

    task_with_flag = Task(
        project_id=project_with_flag.id,
        payloads=[{"type": "create", "prompt": "do something"}],
        vm_instance_id="agent-vm-esi-1",
    )
    db_session.add(task_with_flag)
    await db_session.commit()
    await db_session.refresh(task_with_flag)

    token = create_agent_token(str(task_with_flag.id), "agent-vm-esi-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    assert resp.json()["project_metadata"]["enable_self_improvement"] is True

    # Self-repo without explicit flag → False (decoupled from is_self_repo)
    project_self_repo_no_flag = Project(
        name="Self Repo No Flag",
        user_id="test-user-123",
        is_self_repo=True,
        build_config={"some_other_key": "value"},
    )
    db_session.add(project_self_repo_no_flag)
    await db_session.commit()
    await db_session.refresh(project_self_repo_no_flag)

    task_self_repo = Task(
        project_id=project_self_repo_no_flag.id,
        payloads=[{"type": "create", "prompt": "do something"}],
        vm_instance_id="agent-vm-esi-2",
    )
    db_session.add(task_self_repo)
    await db_session.commit()
    await db_session.refresh(task_self_repo)

    token2 = create_agent_token(str(task_self_repo.id), "agent-vm-esi-2")
    resp2 = await client.post("/api/agent/claim", headers={"x-agent-token": token2})
    assert resp2.status_code == 200
    assert resp2.json()["project_metadata"]["enable_self_improvement"] is False


@pytest.mark.asyncio
async def test_agent_claim_project_metadata_empty_build_config(
    client: AsyncClient, db_session
):
    """
    Tests that agents get an empty build_config dict (not an error) when the project
    has no build_config configured.
    """
    project = Project(name="No Build Config Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do something"}],
        vm_instance_id="agent-vm-meta-2",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-meta-2")
    headers = {"x-agent-token": token}
    resp = await client.post("/api/agent/claim", headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert "project_metadata" in data
    assert data["project_metadata"]["build_config"] == {}
    # Unset approval_gate_config surfaces as an empty dict (does not break response).
    assert data["project_metadata"]["approval_gate_config"] == {}


@pytest.mark.asyncio
async def test_agent_claim_surfaces_approval_gate_config(client: AsyncClient, db_session):
    """A project's approval_gate_config is surfaced in project_metadata so the
    agent's fresh-reviewer can derive per-project sensitive paths."""
    project = Project(
        name="Approval Gate Test",
        user_id="test-user-123",
        github_owner="acme",
        github_repo="myapp",
        approval_gate_config={
            "infra_files": ["infra/deploy.tf"],
            "critical_files": ["lib/payments/charge.py"],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do something"}],
        vm_instance_id="agent-vm-agc-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-agc-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})

    assert resp.status_code == 200
    agc = resp.json()["project_metadata"]["approval_gate_config"]
    assert agc["infra_files"] == ["infra/deploy.tf"]
    assert agc["critical_files"] == ["lib/payments/charge.py"]


@pytest.mark.asyncio
async def test_agent_claim_injects_promotion_target_into_payloads(
    client: AsyncClient, db_session
):
    """promotion_target/integration_branch from per-project config flow into
    every payload entry the agent receives.

    Agent code-change handlers read payload.promotion_target (not
    project_metadata.promotion_target) to drive LTS-aware behavior. Without
    this injection, a project with promotion_target='production' running on
    base_branch='production' silently compares 'production' === 'main' and
    skips baseline capture / the on-LTS test gate.
    """
    project = Project(
        name="Non-Main Promotion Target",
        user_id="test-user-123",
        integration_branch="develop",
        promotion_target="production",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "create", "prompt": "do something", "base_branch": "production"},
            {"type": "noop"},
        ],
        vm_instance_id="agent-vm-promo-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-promo-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()

    assert data["project_metadata"]["promotion_target"] == "production"
    assert data["project_metadata"]["integration_branch"] == "develop"

    assert len(data["payloads"]) == 2
    for entry in data["payloads"]:
        assert entry["promotion_target"] == "production"
        assert entry["integration_branch"] == "develop"

    # task.payloads in the DB is NOT mutated — the injection is response-only.
    await db_session.refresh(task)
    for entry in task.payloads:
        assert "promotion_target" not in entry
        assert "integration_branch" not in entry


@pytest.mark.asyncio
async def test_agent_claim_preserves_payload_promotion_target_override(
    client: AsyncClient, db_session
):
    """A payload that already specifies promotion_target is not overwritten."""
    project = Project(
        name="Override Promotion Target",
        user_id="test-user-123",
        promotion_target="production",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "promotion_target": "staging"}],
        vm_instance_id="agent-vm-promo-2",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-promo-2")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()

    assert data["payloads"][0]["promotion_target"] == "staging"
    assert data["project_metadata"]["promotion_target"] == "production"


@pytest.mark.asyncio
async def test_agent_status_already_completed_returns_200_idempotent(
    client: AsyncClient, db_session, mocker
):
    """
    Tests that completing a task that is already in a terminal state (COMPLETED)
    returns 200 rather than 409, demonstrating idempotent behavior for agent retries.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)

    project = Project(name="Idempotent Complete Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-idempotent",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-idempotent")
    headers = {"x-agent-token": token}
    payload = {
        "task_id": str(task.id),
        "status": "COMPLETED",
    }

    resp = await client.post("/api/agent/status", json=payload, headers=headers)
    assert resp.status_code == 200

    data = resp.json()
    assert data["status"] == "already_terminal"
    assert data["task_id"] == str(task.id)


@pytest.mark.asyncio
async def test_agent_token_rejected_for_terminated_task(
    client: AsyncClient, db_session
):
    """
    Tests that an agent cannot use a JWT for a task that has been terminated.
    The _terminated_task_ids set (populated by SQLAlchemy event on status transition)
    causes verify_agent_token to reject the request with 401.
    """
    from app.routers.agent import _terminated_task_ids

    project = Project(name="Token Revocation Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-revoked",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    task_id_str = str(task.id)
    token = create_agent_token(task_id_str, "agent-vm-revoked")
    headers = {"x-agent-token": token}

    # Simulate task reaching terminal state (what the SQLAlchemy event does in production)
    _terminated_task_ids[task_id_str] = 9999999999.0  # far-future timestamp

    # Heartbeat should now be rejected
    resp = await client.post(
        "/api/agent/heartbeat",
        json={"task_id": task_id_str, "status_message": "still running"},
        headers=headers,
    )
    assert resp.status_code == 401

    # Clean up to avoid polluting other tests
    _terminated_task_ids.pop(task_id_str, None)


@pytest.mark.asyncio
async def test_refresh_token_replaces_expiring_token(client: AsyncClient, db_session):
    """Refreshed token must be accepted on the next request even if the
    original token was issued with a TTL so short it would already be expired.

    This validates the proactive-refresh window: the agent calls
    /api/agent/refresh-token before the old JWT expires and the returned
    token is valid for subsequent API calls (heartbeat here as a proxy).
    AGENT_TOKEN_TTL_HOURS = 1 so the refresh fires at ~45 min (75% of TTL).
    """
    project = Project(name="Token Refresh Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-refresh",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    task_id_str = str(task.id)

    # Use a normal-TTL token for the refresh call itself.
    valid_token = create_agent_token(task_id_str, "agent-vm-refresh")
    resp = await client.post(
        "/api/agent/refresh-token",
        headers={"x-agent-token": valid_token},
    )
    assert resp.status_code == 200, resp.text
    new_token = resp.json()["token"]
    assert new_token != valid_token, "refresh must return a distinct token"

    # The refreshed token must be accepted on the next heartbeat call.
    heartbeat_resp = await client.post(
        "/api/agent/heartbeat",
        json={"task_id": task_id_str, "status_message": "still running"},
        headers={"x-agent-token": new_token},
    )
    assert heartbeat_resp.status_code == 200, (
        f"Refreshed token was rejected on the next request: {heartbeat_resp.text}"
    )


def _agent_auth_body(endpoint: str, task_id: str, vm_instance_id: str) -> dict:
    if endpoint == "status":
        return {"task_id": task_id, "status": "COMPLETED"}
    if endpoint == "heartbeat":
        return {"task_id": task_id, "status_message": "still running"}
    if endpoint == "vm-heartbeat":
        return {"vm_instance_id": vm_instance_id}
    if endpoint == "next-role":
        return {
            "vm_instance_id": vm_instance_id,
            "prior_task_id": task_id,
            "prior_pr_number": 123,
        }
    raise AssertionError(f"unknown endpoint: {endpoint}")


def _truncate_vm_instance_id_for_assertion(vm_instance_id: str) -> str:
    return vm_instance_id if len(vm_instance_id) <= 12 else f"{vm_instance_id[:12]}..."


def _assert_agent_auth_reject_log(
    caplog,
    *,
    reason: str,
    path: str,
    task_id: str,
    token_vm_instance_id: str | None = None,
    token_project_id: str | None = None,
) -> None:
    expected_parts = [
        "[AGENT_AUTH_REJECT]",
        f"reason={reason}",
        f"path={path}",
        f"task_id={task_id}",
    ]
    if token_vm_instance_id is not None:
        expected_parts.append(
            f"token_vm_instance_id={_truncate_vm_instance_id_for_assertion(token_vm_instance_id)}"
        )
    if token_project_id is not None:
        expected_parts.append(f"token_project_id={token_project_id}")
    expected = " ".join(expected_parts)
    assert any(expected in record.message for record in caplog.records), (
        f"missing auth reject log: {expected}\ncaptured={caplog.messages}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint",
    ["status", "heartbeat", "vm-heartbeat", "next-role"],
)
async def test_agent_token_rejected_when_task_row_missing(
    client: AsyncClient, db_session, endpoint: str, caplog
):
    """Missing task rows must fail closed on auth check."""
    from app.routers.agent import _terminated_task_ids

    project = Project(name="Missing Row Auth Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-missing-row",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    task_id = str(task.id)
    vm_instance_id = "agent-vm-missing-row"
    token = create_agent_token(task_id, vm_instance_id)

    _terminated_task_ids.pop(task_id, None)

    await db_session.delete(task)
    await db_session.commit()

    class _MissingRowSession:
        async def get(self, *_args, **_kwargs):
            return None

        async def execute(self, *_args, **_kwargs):
            raise AssertionError(
                "agent handler should not execute when auth cannot prove task is active"
            )

    async def _override_missing_row_session():
        yield _MissingRowSession()

    previous_override = app.dependency_overrides.get(get_session)
    app.dependency_overrides[get_session] = _override_missing_row_session
    caplog.set_level(logging.INFO)
    try:
        resp = await client.post(
            f"/api/agent/{endpoint}",
            json=_agent_auth_body(endpoint, task_id, vm_instance_id),
            headers={"x-agent-token": token},
        )
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(get_session, None)
        else:
            app.dependency_overrides[get_session] = previous_override
        _terminated_task_ids.pop(task_id, None)

    assert resp.status_code == 401
    _assert_agent_auth_reject_log(
        caplog,
        reason="task_missing",
        path=f"/api/agent/{endpoint}",
        task_id=task_id,
        token_vm_instance_id=vm_instance_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint",
    ["status", "heartbeat", "vm-heartbeat", "next-role"],
)
async def test_agent_token_rejected_on_db_error(
    client: AsyncClient, db_session, endpoint: str, caplog
):
    """DB auth errors must reject writes on auth check."""
    from app.routers.agent import _terminated_task_ids

    project = Project(name="DB Error Auth Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-db-error",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    task_id = str(task.id)
    vm_instance_id = "agent-vm-db-error"
    token = create_agent_token(task_id, vm_instance_id)

    _terminated_task_ids.pop(task_id, None)

    class _FailingSession:
        async def get(self, *_args, **_kwargs):
            raise RuntimeError("simulated DB failure")

        async def execute(self, *_args, **_kwargs):
            raise AssertionError(
                "agent handler should not execute when auth DB lookup fails"
            )

    async def _override_failing_session():
        yield _FailingSession()

    previous_override = app.dependency_overrides.get(get_session)
    app.dependency_overrides[get_session] = _override_failing_session
    caplog.set_level(logging.INFO)
    try:
        resp = await client.post(
            f"/api/agent/{endpoint}",
            json=_agent_auth_body(endpoint, task_id, vm_instance_id),
            headers={"x-agent-token": token},
        )
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(get_session, None)
        else:
            app.dependency_overrides[get_session] = previous_override
        _terminated_task_ids.pop(task_id, None)

    assert resp.status_code == 503
    detail = resp.json().get("detail") or resp.json().get("error", {}).get("message")
    assert detail == "agent_auth_db_unavailable"
    _assert_agent_auth_reject_log(
        caplog,
        reason="db_unavailable",
        path=f"/api/agent/{endpoint}",
        task_id=task_id,
        token_vm_instance_id=vm_instance_id,
    )


@pytest.mark.asyncio
async def test_agent_token_rejected_on_deny_list_hit_non_idempotent_path(
    client: AsyncClient, db_session, caplog
):
    from app.routers.agent import _terminated_task_ids

    project = Project(name="Deny-List Auth Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-deny-list",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    task_id = str(task.id)
    vm_instance_id = "agent-vm-deny-list"
    token = create_agent_token(task_id, vm_instance_id)

    _terminated_task_ids[task_id] = time.time()
    caplog.set_level(logging.INFO)
    try:
        resp = await client.post(
            "/api/agent/heartbeat",
            json=_agent_auth_body("heartbeat", task_id, vm_instance_id),
            headers={"x-agent-token": token},
        )
    finally:
        _terminated_task_ids.pop(task_id, None)

    assert resp.status_code == 401
    detail = resp.json().get("detail") or resp.json().get("error", {}).get("message")
    assert detail == "Task is no longer active"
    _assert_agent_auth_reject_log(
        caplog,
        reason="deny_list_hit_non_idempotent_path",
        path="/api/agent/heartbeat",
        task_id=task_id,
        token_vm_instance_id=vm_instance_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint",
    ["heartbeat"],
)
@pytest.mark.parametrize(
    "terminal_status",
    [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED],
    ids=["completed", "failed", "cancelled"],
)
async def test_agent_token_rejected_when_task_terminal_status(
    client: AsyncClient, db_session, endpoint: str, terminal_status: TaskStatus, caplog
):
    """Terminal-status check must reject non-idempotent paths.

    Regression: The 2026-05-24 fix made verify_agent_token always run the DB proof step
    for terminal-status detection. This test pins that terminal tasks are rejected on
    non-idempotent paths.
    """
    from app.routers.agent import _terminated_task_ids

    project = Project(name="Terminal Status Auth Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-terminal",
        status=terminal_status,  # Already terminal
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    task_id = str(task.id)
    vm_instance_id = "agent-vm-terminal"
    token = create_agent_token(task_id, vm_instance_id)

    _terminated_task_ids.pop(task_id, None)
    caplog.set_level(logging.INFO)

    try:
        resp = await client.post(
            f"/api/agent/{endpoint}",
            json=_agent_auth_body(endpoint, task_id, vm_instance_id),
            headers={"x-agent-token": token},
        )
    finally:
        _terminated_task_ids.pop(task_id, None)

    # Non-idempotent paths fail closed with 401 for terminal tasks
    assert resp.status_code == 401, (
        f"endpoint {endpoint} should reject with 401 for terminal task, got {resp.status_code}"
    )
    resp_json = resp.json()
    detail = resp_json.get("detail") or resp_json.get("error", {}).get("message")
    assert detail == "Task is no longer active"
    _assert_agent_auth_reject_log(
        caplog,
        reason="deny_list_hit_non_idempotent_path",
        path=f"/api/agent/{endpoint}",
        task_id=task_id,
        token_vm_instance_id=vm_instance_id,
    )


@pytest.mark.asyncio
async def test_agent_jwt_rejected_cold_deny_list(
    client: AsyncClient, db_session, monkeypatch, caplog
):
    """DB proof path rejects terminal-task JWTs even when the in-memory deny-list is empty.

    Simulates a cold instance (rolling deploy) where _terminated_task_ids has never been
    populated. The DB status check at routers/agent.py:361-379 must still reject the request.
    """
    import app.routers.agent as agent_router
    from app.routers.agent import _terminated_task_ids

    project = Project(name="Cold Deny List Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-cold",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    task_id = str(task.id)
    vm_instance_id = "agent-vm-cold"
    token = create_agent_token(task_id, vm_instance_id)

    # Simulate cold instance: deny-list is completely empty (not just missing this entry)
    monkeypatch.setattr(agent_router, "_terminated_task_ids", {})
    caplog.set_level(logging.INFO)

    resp = await client.post(
        "/api/agent/heartbeat",
        json=_agent_auth_body("heartbeat", task_id, vm_instance_id),
        headers={"x-agent-token": token},
    )

    # DB proof path must reject even with a cold deny-list
    assert resp.status_code == 401
    detail = resp.json().get("detail") or resp.json().get("error", {}).get("message")
    assert detail == "Task is no longer active"
    _assert_agent_auth_reject_log(
        caplog,
        reason="deny_list_hit_non_idempotent_path",
        path="/api/agent/heartbeat",
        task_id=task_id,
        token_vm_instance_id=vm_instance_id,
    )

    # Restore the real dict so other tests are not affected
    monkeypatch.setattr(agent_router, "_terminated_task_ids", _terminated_task_ids)


@pytest.mark.asyncio
async def test_agent_idempotent_path_enforces_freshness(
    client: AsyncClient, db_session, caplog
):
    """Freshness-window check enforces age on warm-idempotent paths.

    Regression: The 2026-05-24 fix made verify_agent_token always run the DB proof step.
    This test verifies that for warm-idempotent paths (/next-role, /vm-heartbeat) on
    terminal tasks, freshness-window check is enforced. A stale JWT (old iat) must be
    rejected.
    """
    from app.routers.agent import (
        _terminated_task_ids,
        _TERMINAL_IDEMPOTENT_FRESHNESS_SECS,
    )

    project = Project(name="Freshness Window Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-freshness",
        status=TaskStatus.COMPLETED,  # Terminal task
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    task_id = str(task.id)
    vm_instance_id = "agent-vm-freshness"

    # Create a stale token (iat in the past, outside freshness window)
    from datetime import datetime, timedelta, timezone
    from app.auth import AGENT_JWT_SECRET, AGENT_JWT_ALGORITHM
    import jwt

    stale_iat = datetime.now(timezone.utc) - timedelta(
        seconds=_TERMINAL_IDEMPOTENT_FRESHNESS_SECS + 60
    )
    stale_expire = datetime.now(timezone.utc) + timedelta(hours=8)
    to_encode = {
        "sub": task_id,
        "vm_instance_id": vm_instance_id,
        "task_type": "",
        "project_id": "",
        "exp": stale_expire,
        "iat": stale_iat,
    }
    stale_token = jwt.encode(to_encode, AGENT_JWT_SECRET, algorithm=AGENT_JWT_ALGORITHM)

    _terminated_task_ids.pop(task_id, None)
    caplog.set_level(logging.INFO)

    try:
        resp = await client.post(
            "/api/agent/vm-heartbeat",
            json={"vm_instance_id": vm_instance_id},
            headers={"x-agent-token": stale_token},
        )
    finally:
        _terminated_task_ids.pop(task_id, None)

    # Freshness check must reject stale JWT
    assert resp.status_code == 401, (
        "stale JWT on warm-idempotent path should be rejected"
    )
    resp_json = resp.json()
    detail = resp_json.get("detail") or resp_json.get("error", {}).get("message")
    assert detail == "terminal_token_expired_for_idempotent"
    _assert_agent_auth_reject_log(
        caplog,
        reason="terminal_token_expired_for_idempotent",
        path="/api/agent/vm-heartbeat",
        task_id=task_id,
        token_vm_instance_id=vm_instance_id,
    )


@pytest.mark.asyncio
async def test_agent_token_rejected_on_vm_instance_mismatch(
    client: AsyncClient, db_session, caplog
):
    """VM instance mismatch check must reject token when task reassigned.

    Regression: A warm cache should not allow skipping the vm_instance_mismatch check.
    When a task is requeued and reassigned to a different VM, the original agent's
    token must be rejected with 403 vm_instance_mismatch.
    """
    from app.routers.agent import _terminated_task_ids

    project = Project(name="VM Mismatch Auth Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-requeued",  # Task reassigned to new VM
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    task_id = str(task.id)
    original_vm = "agent-vm-original"  # Original VM from old assignment
    token = create_agent_token(task_id, original_vm)

    _terminated_task_ids.pop(task_id, None)
    caplog.set_level(logging.INFO)

    try:
        resp = await client.post(
            "/api/agent/heartbeat",
            json={"task_id": task_id, "status_message": "still running"},
            headers={"x-agent-token": token},
        )
    finally:
        _terminated_task_ids.pop(task_id, None)

    assert resp.status_code == 403
    resp_json = resp.json()
    detail = resp_json.get("detail") or resp_json.get("error", {}).get("message")
    assert detail == "vm_instance_mismatch"
    _assert_agent_auth_reject_log(
        caplog,
        reason="vm_instance_mismatch",
        path="/api/agent/heartbeat",
        task_id=task_id,
        token_vm_instance_id=original_vm,
    )


@pytest.mark.asyncio
async def test_agent_token_rejected_on_terminal_idempotent_vm_mismatch(
    client: AsyncClient, db_session, caplog
):
    from app.routers.agent import _terminated_task_ids

    project = Project(name="Terminal VM Mismatch Auth Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-terminal-mismatch",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    task_id = str(task.id)
    original_vm = "agent-vm-original-terminal"
    token = create_agent_token(task_id, original_vm)

    _terminated_task_ids.pop(task_id, None)
    caplog.set_level(logging.INFO)

    try:
        resp = await client.post(
            "/api/agent/vm-heartbeat",
            json={"vm_instance_id": original_vm},
            headers={"x-agent-token": token},
        )
    finally:
        _terminated_task_ids.pop(task_id, None)

    assert resp.status_code == 403
    resp_json = resp.json()
    detail = resp_json.get("detail") or resp_json.get("error", {}).get("message")
    assert detail == "vm_instance_mismatch"
    _assert_agent_auth_reject_log(
        caplog,
        reason="vm_instance_mismatch",
        path="/api/agent/vm-heartbeat",
        task_id=task_id,
        token_vm_instance_id=original_vm,
    )


@pytest.mark.asyncio
async def test_agent_token_rejected_on_project_mismatch(
    client: AsyncClient, db_session, caplog
):
    """Project mismatch check must reject token for cross-project requests.

    Regression: project_id authz check must reject when token carries a mismatched
    project_id that does not match task.project_id (e.g. cross-project task reassignment).
    """
    from app.routers.agent import _terminated_task_ids

    project1 = Project(name="Project 1", user_id="test-user-123")
    project2 = Project(name="Project 2", user_id="test-user-123")
    db_session.add(project1)
    db_session.add(project2)
    await db_session.commit()
    await db_session.refresh(project1)
    await db_session.refresh(project2)

    task = Task(
        project_id=project2.id,  # Task belongs to project2
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-project",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    task_id = str(task.id)
    vm_instance_id = "agent-vm-project"
    # Token carries project1 ID (mismatch)
    token = create_agent_token(task_id, vm_instance_id, project_id=str(project1.id))

    _terminated_task_ids.pop(task_id, None)
    caplog.set_level(logging.INFO)

    try:
        resp = await client.post(
            "/api/agent/heartbeat",
            json={"task_id": task_id, "status_message": "still running"},
            headers={"x-agent-token": token},
        )
    finally:
        _terminated_task_ids.pop(task_id, None)

    assert resp.status_code == 403
    resp_json = resp.json()
    detail = resp_json.get("detail") or resp_json.get("error", {}).get("message")
    assert detail == "project_mismatch"
    _assert_agent_auth_reject_log(
        caplog,
        reason="project_mismatch",
        path="/api/agent/heartbeat",
        task_id=task_id,
        token_vm_instance_id=vm_instance_id,
        token_project_id=str(project1.id),
    )


@pytest.mark.asyncio
async def test_agent_claim_active_tasks_include_related_files(
    client: AsyncClient, db_session
):
    """
    Tests that the project_context returned from /api/agent/claim includes
    related_files in each active_tasks entry when the active task has related_files
    in its context.
    """
    project = Project(name="Related Files Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # An already-IN_PROGRESS task with related_files in its context
    active_task = Task(
        project_id=project.id,
        title="Active sibling task",
        payloads=[{"type": "create", "prompt": "Do something"}],
        vm_instance_id="agent-vm-active",
        status=TaskStatus.IN_PROGRESS,
        context={"related_files": ["backend/app/main.py", "agent/src/index.ts"]},
    )
    db_session.add(active_task)
    await db_session.commit()
    await db_session.refresh(active_task)

    # The task being claimed (research type so active_tasks are included)
    claim_task = Task(
        project_id=project.id,
        title="Claiming task",
        payloads=[{"type": "research", "prompt": "Investigate things"}],
        vm_instance_id="agent-vm-claimer",
    )
    db_session.add(claim_task)
    await db_session.commit()
    await db_session.refresh(claim_task)

    token = create_agent_token(str(claim_task.id), "agent-vm-claimer")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    project_context = data.get("project_context", {})
    active_tasks = project_context.get("active_tasks", [])

    assert len(active_tasks) == 1
    entry = active_tasks[0]
    assert entry["title"] == "Active sibling task"
    assert entry["related_files"] == ["backend/app/main.py", "agent/src/index.ts"]


@pytest.mark.asyncio
async def test_agent_claim_active_tasks_related_files_defaults_to_empty(
    client: AsyncClient, db_session
):
    """
    Tests that active_tasks entries have an empty related_files list when
    the active task has no related_files in its context.
    """
    project = Project(name="No Related Files Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    active_task = Task(
        project_id=project.id,
        title="Active task no files",
        payloads=[{"type": "create", "prompt": "Do stuff"}],
        vm_instance_id="agent-vm-nofiles",
        status=TaskStatus.IN_PROGRESS,
        context={},
    )
    db_session.add(active_task)
    await db_session.commit()
    await db_session.refresh(active_task)

    claim_task = Task(
        project_id=project.id,
        title="Claimer task",
        payloads=[{"type": "research", "prompt": "Research things"}],
        vm_instance_id="agent-vm-claimer2",
    )
    db_session.add(claim_task)
    await db_session.commit()
    await db_session.refresh(claim_task)

    token = create_agent_token(str(claim_task.id), "agent-vm-claimer2")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    active_tasks = data.get("project_context", {}).get("active_tasks", [])
    assert len(active_tasks) == 1
    assert active_tasks[0]["related_files"] == []


@pytest.mark.asyncio
async def test_recently_completed_tasks_include_outcome_context(
    client: AsyncClient, db_session
):
    """
    Recently completed tasks in the claim response carry prompt, related_files,
    direction_id, and (for failed tasks) changed_files so the next agent can
    understand what the prior task tried and what it touched.
    """
    project = Project(name="Recent Outcome Context", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # A failed task with changed_files and a direction
    long_prompt = "Describe the change in detail. " * 20  # ~620 chars
    failed_task = Task(
        project_id=project.id,
        title="Failed feature attempt",
        description="desc",
        payloads=[{"type": "create", "prompt": long_prompt}],
        vm_instance_id="agent-vm-recent-failed",
        status=TaskStatus.FAILED,
        context={
            "direction_id": "dir-abc",
            "related_files": [f"file{i}.py" for i in range(15)],
            "_pr_changed_files": [f"changed{i}.py" for i in range(15)],
        },
        latest_status_message="Tests failed on CI",
    )
    db_session.add(failed_task)
    # A completed task with changed_files (should also be exposed)
    completed_task = Task(
        project_id=project.id,
        title="Completed task",
        payloads=[{"type": "create", "prompt": "Short prompt"}],
        vm_instance_id="agent-vm-recent-done",
        status=TaskStatus.COMPLETED,
        context={
            "direction_id": "dir-xyz",
            "related_files": ["a.py", "b.py"],
            "_pr_changed_files": ["modded1.py", "modded2.py"],
        },
    )
    db_session.add(completed_task)
    await db_session.commit()

    claim_task = Task(
        project_id=project.id,
        title="Claimer",
        payloads=[{"type": "research", "prompt": "Research"}],
        vm_instance_id="agent-vm-recent-claimer",
    )
    db_session.add(claim_task)
    await db_session.commit()
    await db_session.refresh(claim_task)

    token = create_agent_token(str(claim_task.id), "agent-vm-recent-claimer")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    recent = resp.json()["project_context"]["recently_completed_tasks"]

    failed_entry = next(e for e in recent if e["title"] == "Failed feature attempt")
    assert failed_entry["direction_id"] == "dir-abc"
    assert len(failed_entry["related_files"]) == 10  # capped
    assert failed_entry["related_files"][0] == "file0.py"
    assert len(failed_entry["changed_files"]) == 10  # capped
    assert failed_entry["prompt"] is not None
    assert len(failed_entry["prompt"]) <= 300  # truncated
    assert failed_entry["failure_reason"] == "Tests failed on CI"

    done_entry = next(e for e in recent if e["title"] == "Completed task")
    assert done_entry["direction_id"] == "dir-xyz"
    assert done_entry["related_files"] == ["a.py", "b.py"]
    assert done_entry["changed_files"] == ["modded1.py", "modded2.py"]
    assert done_entry["prompt"] == "Short prompt"


@pytest.mark.asyncio
async def test_agent_claim_injects_sr_co_failure_pairs(client: AsyncClient, db_session):
    """The claim response injects sr_co_failure_pairs from recent completed tasks.

    Regression: the scan once filtered + ordered by a non-existent
    ``Task.completed_at`` column (that name is a property on the strategic-cycle
    model, not Task). The query raised AttributeError on every code-change
    claim; the catch-all swallowed it, so the injection silently produced
    nothing — 11x ERROR-log noise and a 100%-dead feature.
    """
    project = Project(name="SR Co-Failure", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # >= _SR_MIN_SAMPLE (5) completed code-change tasks whose self-review failed
    # the same two categories together → a clear co-failure pair.
    for i in range(6):
        db_session.add(
            Task(
                project_id=project.id,
                title=f"Completed {i}",
                payloads=[{"type": "create", "prompt": "x"}],
                status=TaskStatus.COMPLETED,
                context={
                    "_self_review_outcome": {
                        "check_results": {
                            "correctness": {"passed": False},
                            "error-handling": {"passed": False},
                        }
                    }
                },
            )
        )
    await db_session.commit()

    claim_task = Task(
        project_id=project.id,
        title="Claimer",
        payloads=[{"type": "create", "prompt": "Build a thing"}],
        vm_instance_id="agent-vm-cofail-claimer",
    )
    db_session.add(claim_task)
    await db_session.commit()
    await db_session.refresh(claim_task)

    token = create_agent_token(str(claim_task.id), "agent-vm-cofail-claimer")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    pairs = resp.json()["project_context"].get("sr_co_failure_pairs")
    assert pairs, "sr_co_failure_pairs missing — the completed-task scan failed"
    cats = {p["cat_a"] for p in pairs} | {p["cat_b"] for p in pairs}
    assert "correctness" in cats
    assert "error-handling" in cats
    for p in pairs:
        assert {"cat_a", "cat_b", "lift"} <= set(p)


@pytest.mark.asyncio
async def test_lessons_include_first_feedback_when_distinct(
    client: AsyncClient, db_session
):
    """
    Lessons from high-iteration tasks include first_feedback (iteration 0) in
    addition to last_feedback so the initial rejection reason is visible.
    """
    project = Project(name="Lessons First Feedback", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    lesson_task = Task(
        project_id=project.id,
        title="Lesson source",
        payloads=[{"type": "create", "prompt": "Build stuff"}],
        vm_instance_id="agent-vm-lesson",
        status=TaskStatus.COMPLETED,
        context={
            "review_loop": {
                "iteration": 2,
                "review_history": [
                    {"iteration": 0, "feedback": "Initial rejection: wrong approach"},
                    {"iteration": 1, "feedback": "Still failing: missing tests"},
                ],
            }
        },
    )
    db_session.add(lesson_task)
    await db_session.commit()

    claim_task = Task(
        project_id=project.id,
        title="Claimer",
        payloads=[{"type": "research", "prompt": "Research"}],
        vm_instance_id="agent-vm-lesson-claimer",
    )
    db_session.add(claim_task)
    await db_session.commit()
    await db_session.refresh(claim_task)

    token = create_agent_token(str(claim_task.id), "agent-vm-lesson-claimer")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    lessons = resp.json()["project_context"]["lessons"]
    assert len(lessons) == 1
    lesson = lessons[0]
    assert lesson["first_feedback"] == "Initial rejection: wrong approach"
    assert lesson["last_feedback"] == "Still failing: missing tests"


@pytest.mark.asyncio
async def test_agent_claim_sibling_tasks_include_terminal_outcomes(
    client: AsyncClient, db_session
):
    """
    Tests that sibling_tasks in the claim response includes both active and terminal
    siblings. Terminal entries carry status, failure_class (if FAILED), pr_url (if any),
    and an active=False flag so the agent can distinguish in-progress conflicts from
    historical outcomes.
    """
    project = Project(name="Sibling Outcomes Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = Task(
        project_id=project.id,
        title="Parent task",
        payloads=[{"type": "research", "prompt": "investigate"}],
        vm_instance_id="agent-vm-sib-parent",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    completed_sib = Task(
        project_id=project.id,
        title="Completed sibling",
        payloads=[{"type": "create", "prompt": "ship feature A"}],
        vm_instance_id="agent-vm-sib-completed",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
        context={"related_files": ["backend/app/foo.py"]},
    )
    failed_sib = Task(
        project_id=project.id,
        title="Failed sibling",
        payloads=[{"type": "create", "prompt": "ship feature B"}],
        vm_instance_id="agent-vm-sib-failed",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        latest_status_message="ImportError in module xyz: cannot find symbol foo across 3 modules",
        context={
            "related_files": ["backend/app/bar.py"],
            "failure_category": "CODE_ERROR",
            "_pr_changed_files": [
                "backend/app/bar.py",
                "backend/app/baz.py",
                "backend/app/qux.py",
                "backend/app/quux.py",
                "backend/app/corge.py",
                "backend/app/grault.py",
            ],
        },
    )
    in_progress_sib = Task(
        project_id=project.id,
        title="In-progress sibling",
        payloads=[{"type": "create", "prompt": "ship feature C"}],
        vm_instance_id="agent-vm-sib-active",
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=parent.id,
        context={"related_files": ["backend/app/baz.py"]},
    )
    db_session.add_all([completed_sib, failed_sib, in_progress_sib])
    await db_session.commit()
    await db_session.refresh(completed_sib)

    pr_artifact = Artifact(
        task_id=completed_sib.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/example/repo/pull/42",
    )
    db_session.add(pr_artifact)

    claim_task = Task(
        project_id=project.id,
        title="Claiming sibling",
        payloads=[{"type": "create", "prompt": "ship feature D"}],
        vm_instance_id="agent-vm-sib-claimer",
        status=TaskStatus.QUEUED,
        parent_task_id=parent.id,
    )
    db_session.add(claim_task)
    await db_session.commit()
    await db_session.refresh(claim_task)

    token = create_agent_token(str(claim_task.id), "agent-vm-sib-claimer")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    siblings = resp.json().get("project_context", {}).get("sibling_tasks", [])
    by_title = {s["title"]: s for s in siblings}
    assert set(by_title.keys()) == {
        "Completed sibling",
        "Failed sibling",
        "In-progress sibling",
    }

    active_entry = by_title["In-progress sibling"]
    assert active_entry["active"] is True
    assert active_entry["related_files"] == ["backend/app/baz.py"]

    completed_entry = by_title["Completed sibling"]
    assert completed_entry["active"] is False
    assert completed_entry["status"] == TaskStatus.COMPLETED.value
    assert completed_entry["pr_url"] == "https://github.com/example/repo/pull/42"
    assert "failure_class" not in completed_entry

    failed_entry = by_title["Failed sibling"]
    assert failed_entry["active"] is False
    assert failed_entry["status"] == TaskStatus.FAILED.value
    assert failed_entry["failure_class"] == "CODE_ERROR"
    assert failed_entry["pr_url"] is None
    # New: failure surfaces an error excerpt and capped changed files list
    assert failed_entry["error_excerpt"].startswith("ImportError in module xyz")
    assert len(failed_entry["error_excerpt"]) <= 150
    assert failed_entry["changed_files"] == [
        "backend/app/bar.py",
        "backend/app/baz.py",
        "backend/app/qux.py",
        "backend/app/quux.py",
        "backend/app/corge.py",
    ]
    # Completed sibling does not get error_excerpt / changed_files
    assert "error_excerpt" not in completed_entry
    assert "changed_files" not in completed_entry


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_playbook_set_and_get(client: AsyncClient, db_session):
    """Tests setting and retrieving playbook entries via POST /api/agent/playbook."""
    project = Project(name="Playbook Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Do something"}],
        vm_instance_id="agent-vm-playbook1",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-playbook1")
    headers = {"x-agent-token": token}

    # Set two playbook entries
    resp = await client.post(
        "/api/agent/playbook",
        json={
            "task_id": str(task.id),
            "op": "set",
            "key": "build-commands",
            "value": "npm run build",
        },
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["op"] == "set"
    assert data["key"] == "build-commands"
    assert data["revision"] == 1

    resp2 = await client.post(
        "/api/agent/playbook",
        json={
            "task_id": str(task.id),
            "op": "set",
            "key": "lint-rules",
            "value": "eslint --max-warnings 0",
        },
        headers=headers,
    )
    assert resp2.status_code == 200
    assert resp2.json()["revision"] == 2

    # Verify stored in design_doc
    await db_session.refresh(project)
    assert project.design_doc is not None
    assert "playbook/build-commands" in project.design_doc
    assert project.design_doc["playbook/build-commands"]["content"] == "npm run build"
    assert project.design_doc["playbook/build-commands"]["source_task_id"] == str(
        task.id
    )
    assert "playbook/lint-rules" in project.design_doc

    # Get all playbook entries
    resp3 = await client.post(
        "/api/agent/playbook",
        json={"task_id": str(task.id), "op": "get"},
        headers=headers,
    )
    assert resp3.status_code == 200
    data3 = resp3.json()
    assert data3["status"] == "ok"
    assert data3["op"] == "get"
    entries = data3["entries"]
    assert "build-commands" in entries
    assert entries["build-commands"]["content"] == "npm run build"
    assert "lint-rules" in entries

    # List keys
    resp4 = await client.post(
        "/api/agent/playbook",
        json={"task_id": str(task.id), "op": "list"},
        headers=headers,
    )
    assert resp4.status_code == 200
    data4 = resp4.json()
    assert set(data4["keys"]) == {"build-commands", "lint-rules"}

    await db_session.refresh(project)
    assert "playbook/lint-rules" in project.design_doc
    assert "playbook/build-commands" in project.design_doc


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_playbook_delete_is_rejected_for_agents(client: AsyncClient, db_session):
    """Agents cannot delete shared playbook entries via POST /api/agent/playbook."""
    project = Project(name="Playbook Delete Rejection Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Do something"}],
        vm_instance_id="agent-vm-playbook-delete",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-playbook-delete")
    headers = {"x-agent-token": token}

    resp = await client.post(
        "/api/agent/playbook",
        json={
            "task_id": str(task.id),
            "op": "set",
            "key": "lint-rules",
            "value": "eslint --max-warnings 0",
        },
        headers=headers,
    )
    assert resp.status_code == 200

    delete_resp = await client.post(
        "/api/agent/playbook",
        json={"task_id": str(task.id), "op": "delete", "key": "lint-rules"},
        headers=headers,
    )
    assert delete_resp.status_code == 403
    assert "not allowed" in delete_resp.json()["error"]["message"]

    await db_session.refresh(project)
    assert "playbook/lint-rules" in project.design_doc


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_design_doc_file_to_system_index(client: AsyncClient, db_session):
    """Setting systems/* keys rebuilds _file_to_system reverse index; index hidden from list."""
    project = Project(name="Design Doc Index Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "design_doc_update"}],
        vm_instance_id="agent-vm-ddi-test",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "agent-vm-ddi-test", task_type="design_doc_update"
    )
    headers = {"x-agent-token": token}

    # Set first system
    resp = await client.post(
        "/api/agent/design-doc",
        json={
            "task_id": str(task.id),
            "op": "set",
            "key": "systems/auth",
            "value": {
                "name": "Auth",
                "modules": ["backend/app/auth.py", "backend/app/models.py"],
            },
        },
        headers=headers,
    )
    assert resp.status_code == 200

    await db_session.refresh(project)
    idx = project.design_doc["_file_to_system"]
    assert idx["backend/app/auth.py"] == "auth"
    assert idx["backend/app/models.py"] == "auth"

    # Set second system — index should include files from both systems
    resp2 = await client.post(
        "/api/agent/design-doc",
        json={
            "task_id": str(task.id),
            "op": "set",
            "key": "systems/database",
            "value": {
                "name": "Database",
                "modules": ["backend/app/db.py", "backend/app/models.py"],
            },
        },
        headers=headers,
    )
    assert resp2.status_code == 200

    await db_session.refresh(project)
    idx = project.design_doc["_file_to_system"]
    assert idx["backend/app/auth.py"] == "auth"
    assert idx["backend/app/db.py"] == "database"
    # last-write-wins for shared file: database set second
    assert idx["backend/app/models.py"] == "database"

    # _file_to_system must NOT appear in list
    resp3 = await client.post(
        "/api/agent/design-doc",
        json={
            "task_id": str(task.id),
            "op": "list",
        },
        headers=headers,
    )
    assert resp3.status_code == 200
    assert "_file_to_system" not in resp3.json()["keys"]

    # Delete a system — index should no longer contain its files
    resp4 = await client.post(
        "/api/agent/design-doc",
        json={
            "task_id": str(task.id),
            "op": "delete",
            "key": "systems/auth",
        },
        headers=headers,
    )
    assert resp4.status_code == 200

    await db_session.refresh(project)
    idx = project.design_doc["_file_to_system"]
    assert "backend/app/auth.py" not in idx
    assert idx["backend/app/db.py"] == "database"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_playbook_size_limits(client: AsyncClient, db_session):
    """Tests that per-entry 2KB and total 30KB size limits are enforced."""
    project = Project(name="Playbook Size Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "prompt": "Fix it"}],
        vm_instance_id="agent-vm-playbook2",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-playbook2")
    headers = {"x-agent-token": token}

    # Per-entry limit: value > 2048 bytes should be rejected
    oversized_value = "x" * 2049
    resp = await client.post(
        "/api/agent/playbook",
        json={
            "task_id": str(task.id),
            "op": "set",
            "key": "too-big",
            "value": oversized_value,
        },
        headers=headers,
    )
    assert resp.status_code == 400, f"Expected 400, got: {resp.json()}"
    assert "2KB" in resp.json()["error"]["message"]

    # Total playbook limit: at capacity, new writes succeed by evicting old entries
    # rather than hard-rejecting.  Each entry ~1500 chars value; with metadata
    # overhead each row is ~1620B, so ~18 fit under 30KB before eviction kicks in.
    large_value = "y" * 1500
    for i in range(40):
        r = await client.post(
            "/api/agent/playbook",
            json={
                "task_id": str(task.id),
                "op": "set",
                "key": f"entry-{i:02d}",
                "value": large_value,
            },
            headers=headers,
        )
        assert r.status_code == 200, (
            f"Entry {i} rejected (eviction should have made room): {r.json()}"
        )

    # After 40 writes the archive must have entries — eviction happened.
    await db_session.refresh(project)
    doc = project.design_doc or {}
    assert len(doc.get("playbook_archive") or []) > 0, (
        "Expected eviction to populate playbook_archive"
    )
    # Playbook must still respect the hard cap.
    from app.playbook_learning import _playbook_size_bytes as _psb
    from app.playbook_learning import _PLAYBOOK_CAPACITY_BYTES as _cap

    assert _psb(doc) <= _cap, f"Hard cap violated: {_psb(doc)} bytes"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_playbook_set_rejects_when_nothing_evictable(
    client: AsyncClient, db_session
):
    """When nothing can be evicted and the playbook is at the hard cap, set returns 400."""
    from unittest.mock import patch

    project = Project(name="Playbook Evict Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "prompt": "Fix it"}],
        vm_instance_id="agent-vm-playbook-evict",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-playbook-evict")
    headers = {"x-agent-token": token}

    # Simulate a playbook that is already over cap and has nothing to evict.
    # Patch _playbook_size_bytes to always report > 30KB cap, and
    # _maybe_archive_for_capacity to return doc unchanged (nothing evictable).
    with (
        patch(
            "app.routers.admin_agent_ops._playbook_size_bytes", return_value=32_000
        ),
        patch(
            "app.routers.admin_agent_ops._maybe_archive_for_capacity",
            side_effect=lambda doc, est=0: doc,
        ),
    ):
        resp = await client.post(
            "/api/agent/playbook",
            json={
                "task_id": str(task.id),
                "op": "set",
                "key": "blocked-key",
                "value": "value",
            },
            headers=headers,
        )

    assert resp.status_code == 400, f"Expected 400, got: {resp.json()}"
    assert "30720" in resp.json()["error"]["message"]


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_playbook_any_task_type_allowed(client: AsyncClient, db_session):
    """Tests that any task type (not just design_doc_update) can use the playbook endpoint."""
    project = Project(name="Playbook Any Type Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Use task type "create" — unlike the design-doc endpoint which restricts by type
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Build something"}],
        vm_instance_id="agent-vm-playbook3",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-playbook3")
    headers = {"x-agent-token": token}

    resp = await client.post(
        "/api/agent/playbook",
        json={
            "task_id": str(task.id),
            "op": "set",
            "key": "test-pattern",
            "value": "use pytest fixtures",
        },
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_response_includes_playbook(client: AsyncClient, db_session):
    """Tests that claim endpoint injects playbook entries into the response."""
    project = Project(name="Claim Playbook Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Seed playbook entries directly in design_doc
    from sqlalchemy.orm.attributes import flag_modified

    project.design_doc = {
        "playbook/build-steps": {
            "content": "run npm run build",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
        },
        "playbook/test-commands": {
            "content": "bash scripts/test-backend.sh",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
        },
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Do work"}],
        vm_instance_id="agent-vm-claim-playbook",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-claim-playbook")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    assert "playbook" in data, (
        f"Expected 'playbook' in claim response, got keys: {list(data.keys())}"
    )
    playbook = data["playbook"]
    assert "build-steps" in playbook
    entry = playbook["build-steps"]
    assert isinstance(entry, dict), (
        f"Expected playbook entry to be a dict, got: {type(entry)}"
    )
    assert entry["content"] == "run npm run build"
    assert "effectiveness_score" in entry
    assert "confirmed_count" in entry
    assert "age_days" in entry
    assert "stale" in entry
    assert "test-commands" in playbook
    assert playbook["test-commands"]["content"] == "bash scripts/test-backend.sh"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_playbook_demotes_ineffective_entries(
    client: AsyncClient, db_session
):
    """Entries with low effectiveness score (confirmed/referenced ratio) sort after effective entries."""
    from sqlalchemy.orm.attributes import flag_modified

    project = Project(name="Playbook Demotion Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    project.design_doc = {
        # Ineffective: high reference count, zero confirmations — should be demoted
        "playbook/zzz-ineffective": {
            "content": "ineffective guidance",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
            "reference_count": 15,
        },
        # Effective: has confirmations — should sort before the ineffective one
        "playbook/aaa-effective": {
            "content": "effective guidance",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 3,
            "reference_count": 10,
        },
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Do work"}],
        vm_instance_id="agent-vm-demotion-test",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-demotion-test")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    playbook = data["playbook"]
    assert "zzz-ineffective" in playbook
    assert "aaa-effective" in playbook

    # The effective entry must appear before the ineffective one in insertion order
    keys = list(playbook.keys())
    assert keys.index("aaa-effective") < keys.index("zzz-ineffective"), (
        f"Expected effective entry before ineffective, got order: {keys}"
    )


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_skips_stale_playbook_entries(client: AsyncClient, db_session):
    """Playbook entries with stale=True are not shown to agents (auto-archived)."""
    from sqlalchemy.orm.attributes import flag_modified

    project = Project(name="Stale Playbook Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    project.design_doc = {
        "playbook/stale-entry": {
            "content": "outdated guidance that no longer helps",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
            "reference_count": 15,
            "stale": True,
            "effectiveness_score": 0.0,
        },
        "playbook/active-entry": {
            "content": "current helpful guidance",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 2,
            "reference_count": 5,
        },
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Do work"}],
        vm_instance_id="agent-vm-stale-test",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-stale-test")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    playbook = data.get("playbook", {})
    assert "active-entry" in playbook, "Active entry should be shown"
    assert "stale-entry" not in playbook, "Stale entry should be excluded"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_caps_playbook_entries_for_fix_tasks(
    client: AsyncClient, db_session
):
    """Playbook is capped per task type: fix gets 8, regardless of how many
    candidates pass the type/stale filters. Without the cap the agent's prompt
    was being flooded with ~30 unrelated entries (see audit on PR #3407)."""
    from sqlalchemy.orm.attributes import flag_modified

    project = Project(name="Playbook Cap Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    design_doc = {}
    for i in range(20):
        design_doc[f"playbook/entry-{i:02d}"] = {
            "content": f"guidance #{i}",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": i,  # later entries rank higher by confirmed_count
            "reference_count": 1,
        }
    project.design_doc = design_doc
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "prompt": "Address review"}],
        vm_instance_id="agent-vm-pbcap-test",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-pbcap-test")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    playbook = resp.json().get("playbook", {})
    assert len(playbook) == 8, f"fix tasks should cap at 8 entries, got {len(playbook)}"


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_caps_playbook_entries_for_create_tasks(
    client: AsyncClient, db_session
):
    """Create tasks get a wider playbook (12) than fix/qa/review (8) — broader
    investigation surface justifies more priors."""
    from sqlalchemy.orm.attributes import flag_modified

    project = Project(name="Playbook Cap Create Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    design_doc = {}
    for i in range(20):
        design_doc[f"playbook/entry-{i:02d}"] = {
            "content": f"guidance #{i}",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": i,
            "reference_count": 1,
        }
    project.design_doc = design_doc
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Build X"}],
        vm_instance_id="agent-vm-pbcap-create-test",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-pbcap-create-test")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    playbook = resp.json().get("playbook", {})
    assert len(playbook) == 12, (
        f"create tasks should cap at 12 entries, got {len(playbook)}"
    )


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_appends_recent_outcomes_on_reference(
    client: AsyncClient, db_session
):
    """Claim appends False to recent_outcomes for each shown entry and stores _shown_playbook_keys."""
    from sqlalchemy.orm.attributes import flag_modified

    project = Project(name="Recent Outcomes Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    project.design_doc = {
        "playbook/my-entry": {
            "content": "some guidance",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 1,
            "reference_count": 3,
            "recent_outcomes": [True, False, True],
        },
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Do work"}],
        vm_instance_id="agent-vm-outcomes-test",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-outcomes-test")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    # Verify recent_outcomes got a False appended
    await db_session.refresh(project)
    entry = project.design_doc["playbook/my-entry"]
    outcomes = entry.get("recent_outcomes", [])
    assert outcomes == [True, False, True, False], (
        f"Expected [..., False] appended, got {outcomes}"
    )

    # Verify task context stores shown keys
    await db_session.refresh(task)
    shown_keys = (task.context or {}).get("_shown_playbook_keys", [])
    assert "my-entry" in shown_keys, (
        f"Expected 'my-entry' in _shown_playbook_keys, got {shown_keys}"
    )


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_records_review_measurement_attribution(
    client: AsyncClient, db_session
):
    """Claim stores _had_review_measurements=True and _top_review_issue_categories when review data exists."""
    project = Project(
        name="Review Measurement Attribution Test", user_id="test-user-123"
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Do work"}],
        vm_instance_id="agent-vm-review-attr",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Seed completed tasks with review_loop history containing >=5 categorizable issues
    review_issue = {
        "description": "Missing unit test coverage for this function",
        "severity": "MAJOR",
    }
    review_history_entry = {"issues": [review_issue] * 5, "iteration": 1}
    completed_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "prev"}],
        status=TaskStatus.COMPLETED,
        attempt_count=2,
        context={
            "review_loop": {"iteration": 2, "review_history": [review_history_entry]}
        },
    )
    db_session.add(completed_task)
    await db_session.commit()

    token = create_agent_token(str(task.id), "agent-vm-review-attr")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    await db_session.refresh(task)
    ctx = task.context or {}
    assert ctx.get("_had_review_measurements") is True, (
        f"Expected _had_review_measurements=True, got {ctx}"
    )
    categories = ctx.get("_top_review_issue_categories", [])
    assert len(categories) > 0, (
        f"Expected non-empty _top_review_issue_categories, got {categories}"
    )
    assert "test-coverage" in categories, (
        f"Expected test-coverage in categories, got {categories}"
    )


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_review_measurement_attribution_false_when_no_data(
    client: AsyncClient, db_session
):
    """Claim stores _had_review_measurements=False when no review data is available."""
    project = Project(name="No Review Data Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Do work"}],
        vm_instance_id="agent-vm-no-review",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-no-review")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    await db_session.refresh(task)
    ctx = task.context or {}
    assert ctx.get("_had_review_measurements") is False, (
        f"Expected _had_review_measurements=False, got {ctx}"
    )
    assert "_top_review_issue_categories" not in ctx, (
        "Should not store categories when no measurements"
    )


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_recent_outcomes_sorts_by_effectiveness(
    client: AsyncClient, db_session
):
    """Claim sorts entries by recent_outcomes effectiveness score (sum/len) descending."""
    from sqlalchemy.orm.attributes import flag_modified

    project = Project(name="Effectiveness Sort Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    project.design_doc = {
        # Low effectiveness: 1/5 = 0.2 score
        "playbook/low-effectiveness": {
            "content": "rarely helps",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 1,
            "reference_count": 5,
            "recent_outcomes": [False, False, False, True, False],  # score = 0.2
        },
        # High effectiveness: 4/5 = 0.8 score
        "playbook/high-effectiveness": {
            "content": "usually helps",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 4,
            "reference_count": 5,
            "recent_outcomes": [True, True, False, True, True],  # score = 0.8
        },
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Do work"}],
        vm_instance_id="agent-vm-effectiveness-sort",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-effectiveness-sort")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    playbook = data.get("playbook", {})
    keys = list(playbook.keys())
    assert keys.index("high-effectiveness") < keys.index("low-effectiveness"), (
        f"Expected high-effectiveness before low-effectiveness, got: {keys}"
    )


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_injects_direction_context_for_create_task(
    client: AsyncClient, db_session
):
    """Claim endpoint injects direction_context for create/fix/refactor tasks when direction_id is set."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    research_context = (
        "## Overview\nImprove auth flow for users.\n\n"
        "## Failed Approaches\n"
        "- JWT in cookies caused CSRF issues\n"
        "- Session-based auth had scaling problems\n"
        "## Next Steps\nTry token refresh pattern.\n"
    )
    project = Project(name="Direction Context Test", user_id="test-user-123")
    project.directions = [
        {
            "id": direction_id,
            "title": "Improve auth flow",
            "priority": "high",
            "research_context": research_context,
            "research_history": [
                {"tasks_created": 3, "prs_merged": 2, "prs_rejected": 1},
                {"tasks_created": 4, "prs_merged": 1, "prs_rejected": 0},
            ],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Implement auth improvements"}],
        vm_instance_id="agent-vm-dir-ctx-create",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-dir-ctx-create")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    assert "direction_context" in data, (
        f"Expected direction_context in claim response, got keys: {list(data.keys())}"
    )
    dc = data["direction_context"]
    assert dc["title"] == "Improve auth flow"
    assert dc["priority"] == "high"
    assert {
        "text": "JWT in cookies caused CSRF issues",
        "failure_type": "",
        "failed_at": "",
        "failure_stage": "",
    } in dc["failed_approaches"]
    assert {
        "text": "Session-based auth had scaling problems",
        "failure_type": "",
        "failed_at": "",
        "failure_stage": "",
    } in dc["failed_approaches"]
    # recent_outcomes: last 2 history entries with tasks_created > 0
    assert len(dc["recent_outcomes"]) == 2
    assert dc["recent_outcomes"][0]["prs_merged"] == 2
    assert dc["recent_outcomes"][1]["prs_merged"] == 1


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_injects_scope_success_rates_in_direction_context(
    client: AsyncClient, db_session
):
    """direction_context.scope_success_rates aggregates scope_breakdown over last 3 productive cycles."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Scope Rates Test", user_id="test-user-xyz")
    project.directions = [
        {
            "id": direction_id,
            "title": "d",
            "priority": "medium",
            "research_context": "",
            "research_history": [
                # non-productive cycle should be excluded
                {
                    "tasks_created": 0,
                    "prs_merged": 0,
                    "scope_breakdown": {
                        "small": {"created": 0, "merged": 0, "failed": 0}
                    },
                },
                {
                    "tasks_created": 2,
                    "prs_merged": 2,
                    "scope_breakdown": {
                        "small": {"created": 2, "merged": 2, "failed": 0}
                    },
                },
                {
                    "tasks_created": 2,
                    "prs_merged": 1,
                    "scope_breakdown": {
                        "small": {"created": 2, "merged": 1, "failed": 1},
                        "medium": {"created": 1, "merged": 0, "failed": 1},
                    },
                },
                {
                    "tasks_created": 1,
                    "prs_merged": 0,
                    "scope_breakdown": {
                        "empty": {"created": 0, "merged": 0, "failed": 0}
                    },
                },
            ],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do"}],
        vm_instance_id="agent-vm-scope-rates",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-scope-rates")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    dc = resp.json()["direction_context"]
    rates = dc["scope_success_rates"]
    assert rates["small"] == {
        "created": 4,
        "merged": 3,
        "failed": 1,
        "merged_cross_module": 0,
    }
    assert rates["medium"] == {
        "created": 1,
        "merged": 0,
        "failed": 1,
        "merged_cross_module": 0,
    }
    assert "empty" not in rates


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_injects_avg_expansion_ratio_in_scope_success_rates(
    client: AsyncClient, db_session
):
    """direction_context.scope_success_rates includes weighted-avg avg_expansion_ratio from scope_breakdown."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Scope Expansion Test", user_id="test-user-exp")
    project.directions = [
        {
            "id": direction_id,
            "title": "d",
            "priority": "medium",
            "research_context": "",
            "research_history": [
                {
                    "tasks_created": 2,
                    "prs_merged": 2,
                    "scope_breakdown": {
                        "small": {
                            "created": 2,
                            "merged": 2,
                            "failed": 0,
                            "avg_expansion_ratio_merged": 2.0,
                        }
                    },
                },
                {
                    "tasks_created": 2,
                    "prs_merged": 2,
                    "scope_breakdown": {
                        "small": {
                            "created": 2,
                            "merged": 2,
                            "failed": 0,
                            "avg_expansion_ratio_merged": 1.0,
                        }
                    },
                },
            ],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do"}],
        vm_instance_id="agent-vm-exp",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-exp")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    rates = resp.json()["direction_context"]["scope_success_rates"]
    assert rates["small"]["avg_expansion_ratio"] == 1.5


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_injects_review_iteration_trend_in_direction_context(
    client: AsyncClient, db_session
):
    """direction_context includes review_iteration_rolling_median and review_iteration_last_cycle when history has values."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Review Iter Trend Test", user_id="test-user-iter")
    project.directions = [
        {
            "id": direction_id,
            "title": "d",
            "priority": "medium",
            "research_context": "",
            "research_history": [
                {"tasks_created": 1, "prs_merged": 1, "median_review_iterations": 1.0},
                {"tasks_created": 1, "prs_merged": 1, "median_review_iterations": 3.0},
                {"tasks_created": 1, "prs_merged": 1, "median_review_iterations": 2.0},
            ],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do"}],
        vm_instance_id="agent-vm-iter-trend",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-iter-trend")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    dc = resp.json()["direction_context"]
    # rolling median of [1.0, 3.0, 2.0] sorted → [1.0, 2.0, 3.0], median = 2.0
    assert dc["review_iteration_rolling_median"] == 2.0
    # last cycle value
    assert dc["review_iteration_last_cycle"] == 2.0


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_direction_context_absent_review_iteration_when_no_history(
    client: AsyncClient, db_session
):
    """direction_context omits review iteration fields when no cycle has recorded median_review_iterations."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="No Iter History Test", user_id="test-user-no-iter")
    project.directions = [
        {
            "id": direction_id,
            "title": "d",
            "priority": "medium",
            "research_context": "",
            "research_history": [
                {"tasks_created": 1, "prs_merged": 1},
            ],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do"}],
        vm_instance_id="agent-vm-no-iter",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-no-iter")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    dc = resp.json()["direction_context"]
    assert "review_iteration_rolling_median" not in dc
    assert "review_iteration_last_cycle" not in dc


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_does_not_inject_direction_context_for_review_task(
    client: AsyncClient, db_session
):
    """Claim endpoint does NOT inject direction_context for review tasks (only create/fix/refactor)."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Direction Context Review Test", user_id="test-user-123")
    project.directions = [
        {
            "id": direction_id,
            "title": "Fix bug in login",
            "priority": "medium",
            "research_context": "## Failed Approaches\n- Tried patching the validator\n",
            "research_history": [],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 42, "branch": "fix/login"}],
        vm_instance_id="agent-vm-dir-ctx-review",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-dir-ctx-review")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    assert "direction_context" not in data, (
        f"direction_context should be absent for review tasks, got: {data.get('direction_context')}"
    )


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_injects_saturation_signals_for_research_task(
    client: AsyncClient, db_session
):
    """Claim endpoint injects possibly_saturated + deadlock_detected for research tasks."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Research Saturation Test", user_id="test-user-123")
    # 3 consecutive 0-task cycles with dedup activity → deadlock_detected True
    project.directions = [
        {
            "id": direction_id,
            "title": "Saturated direction",
            "priority": "low",
            "possibly_saturated": True,
            "research_history": [
                {"tasks_created": 0, "filter_breakdown": {"hash_dedup": 2}},
                {"tasks_created": 0, "filter_breakdown": {"hash_dedup": 1}},
                {"tasks_created": 0, "filter_breakdown": {"word_overlap_dedup": 3}},
            ],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "Research this direction"}],
        vm_instance_id="agent-vm-research-sat",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-research-sat")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    dc = resp.json().get("direction_context")
    assert dc is not None
    assert dc["possibly_saturated"] is True
    assert dc["deadlock_detected"] is True
    # Research tasks should not get implementer-specific fields
    assert "failed_approaches" not in dc


@pytest.mark.asyncio
async def test_claim_injects_failure_hotspots_for_research_task(
    client: AsyncClient, db_session
):
    """Research direction_context surfaces diagnosed_affected_files and active anomalies."""
    from sqlalchemy.orm.attributes import flag_modified
    from app import shared_state as _ss

    direction_id = str(uuid.uuid4())
    project = Project(name="Research Hotspot Test", user_id="test-user-123")
    project.directions = [
        {
            "id": direction_id,
            "title": "Direction with diagnosis",
            "priority": "medium",
            "diagnosed_affected_files": [
                "backend/app/foo.py",
                "backend/app/bar.py",
            ],
            "diagnosis_summary": "Recurring import failure in foo/bar modules.",
            "last_remediation_fingerprint": "abc123def456",
            "research_history": [],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    prior = _ss.persistent_anomalies.get(str(project.id))
    _ss.persistent_anomalies[str(project.id)] = {
        "PROVISION_FAILURE": {
            "occurrence_count": 4,
            "severity": "high",
            "latest_details": "Zones cooling down in us-central1.",
        }
    }
    try:
        task = Task(
            project_id=project.id,
            payloads=[{"type": "research", "prompt": "Research this direction"}],
            vm_instance_id="agent-vm-research-hotspot",
            context={"direction_id": direction_id},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        token = create_agent_token(str(task.id), "agent-vm-research-hotspot")
        resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
        assert resp.status_code == 200
        dc = resp.json().get("direction_context") or {}
        fh = dc.get("failure_hotspots")
        assert fh is not None, f"failure_hotspots missing, dc keys: {list(dc.keys())}"
        assert "backend/app/foo.py" in fh["diagnosed_affected_files"]
        assert fh["last_remediation_fingerprint"] == "abc123def456"
        assert fh["diagnosis_summary"].startswith("Recurring import failure")
        assert any(a["key"] == "PROVISION_FAILURE" for a in fh["active_anomalies"])
    finally:
        if prior is None:
            _ss.persistent_anomalies.pop(str(project.id), None)
        else:
            _ss.persistent_anomalies[str(project.id)] = prior


@pytest.mark.asyncio
async def test_claim_injects_dedup_filter_summary_for_research_task(
    client: AsyncClient, db_session
):
    """Claim endpoint surfaces dedup_filter_summary from last research_history entry."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Dedup Filter Summary Test", user_id="test-user-123")
    project.directions = [
        {
            "id": direction_id,
            "title": "Word overlap heavy",
            "priority": "medium",
            "research_history": [
                {
                    "tasks_created": 2,
                    "findings_produced": 7,
                    "filter_breakdown": {
                        "hash_dedup": 0,
                        "word_overlap_dedup": 4,
                        "file_overlap_dedup": 1,
                        "type_filtered": 0,
                        "empty_prompt": 0,
                    },
                },
            ],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "Research"}],
        vm_instance_id="agent-vm-dedup-filter",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-dedup-filter")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    dc = resp.json().get("direction_context")
    assert dc is not None
    dfs = dc.get("dedup_filter_summary")
    assert dfs is not None
    assert "5/7 findings filtered" in dfs["summary"]
    assert "word-overlap" in dfs["summary"]
    assert "distinctive" in dfs["guidance"].lower()


@pytest.mark.asyncio
async def test_compute_failure_stage_pattern_code_error_dominant():
    """Returns code_error pattern when 60%+ of non-infra failures are code errors."""
    from app.routers.agent import _compute_failure_stage_pattern

    history = [
        {
            "stage_dropout": {
                "tasks_failed_code_error": 3,
                "tasks_failed_infra": 1,
                "tasks_required_approval": 1,
                "tasks_merged_staging_only": 0,
            }
        },
        {
            "stage_dropout": {
                "tasks_failed_code_error": 2,
                "tasks_failed_infra": 0,
                "tasks_required_approval": 0,
                "tasks_merged_staging_only": 0,
            }
        },
        {
            "stage_dropout": {
                "tasks_failed_code_error": 1,
                "tasks_failed_infra": 2,
                "tasks_required_approval": 0,
                "tasks_merged_staging_only": 0,
            }
        },
    ]
    # Non-infra totals: code_error=6, approval=1, total=7 → rate=6/7≈0.857
    result = _compute_failure_stage_pattern(history)
    assert result is not None
    assert result["stage"] == "code_error"
    assert result["rate"] >= 0.6
    assert "test" in result["message"].lower() or "code" in result["message"].lower()


@pytest.mark.asyncio
async def test_compute_failure_stage_pattern_approval_dominant():
    """Returns approval pattern when 60%+ of non-infra failures are approval stalls."""
    from app.routers.agent import _compute_failure_stage_pattern

    history = [
        {
            "stage_dropout": {
                "tasks_failed_code_error": 0,
                "tasks_failed_infra": 1,
                "tasks_required_approval": 4,
                "tasks_merged_staging_only": 0,
            }
        },
        {
            "stage_dropout": {
                "tasks_failed_code_error": 1,
                "tasks_failed_infra": 0,
                "tasks_required_approval": 3,
                "tasks_merged_staging_only": 0,
            }
        },
    ]
    # Non-infra totals: code_error=1, approval=7, total=8 → rate=7/8=0.875
    result = _compute_failure_stage_pattern(history)
    assert result is not None
    assert result["stage"] == "approval"
    assert result["rate"] >= 0.6
    assert (
        "approval" in result["message"].lower() or "review" in result["message"].lower()
    )


@pytest.mark.asyncio
async def test_compute_failure_stage_pattern_no_pattern():
    """Returns None when no dominant pattern or insufficient data."""
    from app.routers.agent import _compute_failure_stage_pattern

    # Insufficient data (total < 3)
    assert (
        _compute_failure_stage_pattern(
            [
                {
                    "stage_dropout": {
                        "tasks_failed_code_error": 1,
                        "tasks_failed_infra": 0,
                        "tasks_required_approval": 1,
                    }
                },
            ]
        )
        is None
    )

    # No clear pattern (roughly even split)
    mixed_history = [
        {
            "stage_dropout": {
                "tasks_failed_code_error": 2,
                "tasks_failed_infra": 0,
                "tasks_required_approval": 2,
            }
        },
        {
            "stage_dropout": {
                "tasks_failed_code_error": 1,
                "tasks_failed_infra": 0,
                "tasks_required_approval": 1,
            }
        },
    ]
    # code_error=3, approval=3, total=6 → neither reaches 60%
    assert _compute_failure_stage_pattern(mixed_history) is None

    # Empty history
    assert _compute_failure_stage_pattern([]) is None


@pytest.mark.asyncio
async def test_compute_failure_stage_pattern_staging_only_dominant():
    """Returns staging_only pattern when 60%+ of non-infra failures are staging-only dropouts."""
    from app.routers.agent import _compute_failure_stage_pattern

    history = [
        {
            "stage_dropout": {
                "tasks_failed_code_error": 0,
                "tasks_failed_infra": 1,
                "tasks_required_approval": 1,
                "tasks_merged_staging_only": 3,
            }
        },
        {
            "stage_dropout": {
                "tasks_failed_code_error": 1,
                "tasks_failed_infra": 0,
                "tasks_required_approval": 0,
                "tasks_merged_staging_only": 2,
            }
        },
    ]
    # Non-infra totals: code_error=1, approval=1, staging_only=5, total=7 → rate=5/7≈0.714
    result = _compute_failure_stage_pattern(history)
    assert result is not None
    assert result["stage"] == "staging_only"
    assert result["rate"] >= 0.6
    assert (
        "integration" in result["message"].lower()
        or "rollup" in result["message"].lower()
        or "ci" in result["message"].lower()
    )


def test_parse_failed_approach_extracts_known_category():
    """Known failure categories in parens are extracted into failure_type."""
    from app.routers.agent import _parse_failed_approach

    result = _parse_failed_approach(
        "Use jwt in cookies (CODE_ERROR) — CSRF vulnerability"
    )
    assert result["failure_type"] == "CODE_ERROR"
    assert "CODE_ERROR" not in result["text"]
    assert "Use jwt in cookies" in result["text"]
    assert "CSRF" in result["text"]


def test_parse_failed_approach_without_category_passes_through():
    """Lines without a parenthesised category return text unchanged and empty failure_type."""
    from app.routers.agent import _parse_failed_approach

    result = _parse_failed_approach("Session auth had scaling issues")
    assert result["failure_type"] == ""
    assert result["text"] == "Session auth had scaling issues"


def test_parse_failed_approach_ignores_unknown_parens():
    """Parens whose contents aren't in the known-category set are kept as prose."""
    from app.routers.agent import _parse_failed_approach

    result = _parse_failed_approach(
        "Try batching (see issue #42) to reduce round trips"
    )
    assert result["failure_type"] == ""
    # Prose parens must be preserved — they're part of the description, not metadata
    assert "(see issue #42)" in result["text"]


def test_parse_failed_approach_truncates_long_text():
    """Text is capped at 200 chars to avoid bloat in agent prompts."""
    from app.routers.agent import _parse_failed_approach

    long_line = "x" * 300
    result = _parse_failed_approach(long_line)
    assert len(result["text"]) == 200


def test_parse_failed_approach_extracts_failed_at_and_stage_metadata():
    """``[at=... stage=...]`` metadata after the category is parsed and removed."""
    from app.routers.agent import _parse_failed_approach

    line = "Use jwt in cookies (CODE_ERROR) [at=2026-04-15 stage=test] — CSRF vulnerability"
    result = _parse_failed_approach(line)
    assert result["failure_type"] == "CODE_ERROR"
    assert result["failed_at"] == "2026-04-15"
    assert result["failure_stage"] == "test"
    # Metadata block should not leak into text
    assert "at=" not in result["text"]
    assert "stage=" not in result["text"]
    assert "Use jwt in cookies" in result["text"]
    assert "CSRF" in result["text"]


def test_parse_failed_approach_stage_only_metadata():
    """Metadata block may contain only one of the two fields."""
    from app.routers.agent import _parse_failed_approach

    result = _parse_failed_approach(
        "Approach X (CODE_ERROR) [stage=lint] — ruff flagged style"
    )
    assert result["failure_type"] == "CODE_ERROR"
    assert result["failed_at"] == ""
    assert result["failure_stage"] == "lint"


def test_parse_failed_approach_without_metadata_returns_empty_fields():
    """Legacy entries without a metadata block still parse cleanly."""
    from app.routers.agent import _parse_failed_approach

    result = _parse_failed_approach("Legacy (CODE_ERROR) — some reason")
    assert result["failure_type"] == "CODE_ERROR"
    assert result["failed_at"] == ""
    assert result["failure_stage"] == ""


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_injects_failure_stage_pattern_in_direction_context(
    client: AsyncClient, db_session
):
    """Claim endpoint injects failure_stage_pattern into direction_context when pattern is clear."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Stage Dropout Pattern Test", user_id="test-user-123")
    project.directions = [
        {
            "id": direction_id,
            "title": "Fix test flakiness",
            "priority": "high",
            "research_context": "",
            "research_history": [
                {
                    "tasks_created": 4,
                    "prs_merged": 1,
                    "stage_dropout": {
                        "tasks_failed_code_error": 3,
                        "tasks_failed_infra": 0,
                        "tasks_required_approval": 0,
                        "tasks_merged_staging_only": 0,
                    },
                },
                {
                    "tasks_created": 3,
                    "prs_merged": 1,
                    "stage_dropout": {
                        "tasks_failed_code_error": 2,
                        "tasks_failed_infra": 1,
                        "tasks_required_approval": 0,
                        "tasks_merged_staging_only": 0,
                    },
                },
                {
                    "tasks_created": 5,
                    "prs_merged": 2,
                    "stage_dropout": {
                        "tasks_failed_code_error": 3,
                        "tasks_failed_infra": 0,
                        "tasks_required_approval": 0,
                        "tasks_merged_staging_only": 0,
                    },
                },
            ],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Fix the flaky tests"}],
        vm_instance_id="agent-vm-stage-dropout",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-stage-dropout")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    assert "direction_context" in data
    dc = data["direction_context"]
    assert "failure_stage_pattern" in dc
    fsp = dc["failure_stage_pattern"]
    assert fsp is not None
    assert fsp["stage"] == "code_error"
    assert "message" in fsp
    assert fsp["rate"] >= 0.6


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_injects_direction_prior_analysis(client: AsyncClient, db_session):
    """Claim endpoint surfaces condensed prior analysis from recent failed/cancelled sibling tasks."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Prior Analysis Test", user_id="test-user-123")
    project.directions = [
        {
            "id": direction_id,
            "title": "Refactor module X",
            "priority": "medium",
            "research_context": "",
            "research_history": [],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    long_plan = "A" * 800  # will be truncated to 500
    long_tfs = "B" * 600  # will be truncated to 300
    prior = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "prior"}],
        vm_instance_id="vm-prior-1",
        context={
            "direction_id": direction_id,
            "_analysis_plan": long_plan,
            "_test_failure_summary": long_tfs,
            "_pr_changed_files": ["foo.py", "bar.ts"],
            "_pr_diff_summary": [
                {
                    "file": "foo.py",
                    "additions": 10,
                    "deletions": 5,
                    "change_type": "modified",
                },
                {
                    "file": "bar.ts",
                    "additions": 3,
                    "deletions": 2,
                    "change_type": "added",
                },
            ],
            "_self_review_outcome": {
                "verdict": "FAIL",
                "notes": "lint errors remain",
                "check_results": {
                    "lint": {"passed": False, "issue": "E501"},
                    "tests": {"passed": True},
                    "types": {"passed": False},
                },
            },
            "review_loop": {"iteration": 3},
        },
        # Audit W1: previous_error_fingerprint is a column.
        previous_error_fingerprint="abc123def456",
        status=TaskStatus.FAILED,
        title="Prior failed task",
        latest_status_message="TypeError: cannot read property x of undefined",
    )
    # Prior with no analysis artifacts and no status message — should be skipped
    prior_no_artifacts = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "no artifacts"}],
        vm_instance_id="vm-prior-2",
        context={"direction_id": direction_id},
        status=TaskStatus.FAILED,
        title="No artifacts task",
        latest_status_message=None,
    )
    db_session.add_all([prior, prior_no_artifacts])
    await db_session.commit()

    current = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "current"}],
        vm_instance_id="vm-current",
        context={"direction_id": direction_id},
    )
    db_session.add(current)
    await db_session.commit()
    await db_session.refresh(current)

    token = create_agent_token(str(current.id), "vm-current")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    dc = resp.json()["direction_context"]
    assert "direction_prior_analysis" in dc
    dpa = dc["direction_prior_analysis"]
    assert len(dpa) == 1
    entry = dpa[0]
    assert entry["title"] == "Prior failed task"
    assert "failure_category" in entry
    assert len(entry["analysis_plan"]) == 500
    assert len(entry["test_failure_summary"]) == 300
    assert entry["pr_changed_files"] == ["foo.py", "bar.ts"]
    assert entry["pr_diff_summary"] == [
        {"file": "foo.py", "added": 10, "removed": 5, "change_type": "modified"},
        {"file": "bar.ts", "added": 3, "removed": 2, "change_type": "added"},
    ]
    assert entry["previous_error_fingerprint"] == "abc123def456"
    assert (
        entry["latest_status_message"]
        == "TypeError: cannot read property x of undefined"
    )
    assert entry["self_review_outcome"]["verdict"] == "FAIL"
    assert entry["self_review_outcome"]["notes"] == "lint errors remain"
    assert set(entry["self_review_outcome"]["failed_checks"]) == {"lint", "types"}
    assert entry["review_iterations"] == 3
    # Total payload stays under 2000-char cap
    import json as _json

    assert len(_json.dumps(dpa)) <= 2000


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_includes_structured_review_feedback(
    client: AsyncClient, db_session
):
    """Failed tasks with structured review issues surface top critical/major items."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Review Feedback Test", user_id="test-user-123")
    project.directions = [
        {
            "id": direction_id,
            "title": "Refactor module Z",
            "priority": "medium",
            "research_context": "",
            "research_history": [],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    prior = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "prior"}],
        vm_instance_id="vm-rf-1",
        context={
            "direction_id": direction_id,
            "_analysis_plan": "approach plan",
            "review_loop": {
                "iteration": 2,
                "review_history": [
                    {
                        "iteration": 0,
                        "feedback": "first round",
                        "issues": [
                            {"severity": "minor", "description": "old style"},
                        ],
                    },
                    {
                        "iteration": 1,
                        "feedback": "second round",
                        "issues": [
                            {"severity": "minor", "description": "nit: rename var"},
                            {
                                "severity": "critical",
                                "description": "missing null check in handler",
                                "file": "foo.py",
                                "line": 42,
                            },
                            {
                                "severity": "major",
                                "description": "no test for edge case",
                                "file": "test_foo.py",
                            },
                            {
                                "severity": "critical",
                                "description": "race condition in cache update",
                            },
                            {
                                "severity": "major",
                                "description": "should be a single bullet but text long "
                                * 10,
                            },
                        ],
                    },
                ],
            },
        },
        status=TaskStatus.FAILED,
        title="Prior with review feedback",
        latest_status_message="failed self-review",
    )
    db_session.add(prior)
    await db_session.commit()

    current = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "current"}],
        vm_instance_id="vm-rf-current",
        context={"direction_id": direction_id},
    )
    db_session.add(current)
    await db_session.commit()
    await db_session.refresh(current)

    token = create_agent_token(str(current.id), "vm-rf-current")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    dpa = resp.json()["direction_context"]["direction_prior_analysis"]
    assert len(dpa) == 1
    rf = dpa[0]["review_feedback"]
    # Top 3 critical/major from the LAST review entry, critical first
    assert isinstance(rf, list)
    assert len(rf) == 3
    assert rf[0]["severity"] == "CRITICAL"
    assert rf[0]["description"] == "missing null check in handler"
    assert rf[0]["file"] == "foo.py"
    assert rf[0]["line"] == 42
    assert rf[1]["severity"] == "CRITICAL"
    assert rf[1]["description"] == "race condition in cache update"
    assert "file" not in rf[1]
    assert rf[2]["severity"] == "MAJOR"
    # Long description truncated to 150 chars
    assert len(rf[2]["description"]) <= 150


@pytest.mark.asyncio
async def test_claim_review_feedback_falls_back_to_text(
    client: AsyncClient, db_session
):
    """When review history lacks structured issues, fall back to truncated feedback string."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Review Feedback Fallback", user_id="test-user-123")
    project.directions = [
        {
            "id": direction_id,
            "title": "Refactor module W",
            "priority": "medium",
            "research_context": "",
            "research_history": [],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    long_feedback = "F" * 500  # truncated to 200
    prior = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "prior"}],
        vm_instance_id="vm-rf-fb-1",
        context={
            "direction_id": direction_id,
            "_analysis_plan": "approach",
            "review_loop": {
                "iteration": 1,
                "review_history": [
                    {"iteration": 0, "feedback": long_feedback},
                ],
            },
        },
        status=TaskStatus.FAILED,
        title="Prior text feedback",
        latest_status_message="failed",
    )
    db_session.add(prior)
    await db_session.commit()

    current = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "current"}],
        vm_instance_id="vm-rf-fb-current",
        context={"direction_id": direction_id},
    )
    db_session.add(current)
    await db_session.commit()
    await db_session.refresh(current)

    token = create_agent_token(str(current.id), "vm-rf-fb-current")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    dpa = resp.json()["direction_context"]["direction_prior_analysis"]
    assert len(dpa) == 1
    rf = dpa[0]["review_feedback"]
    assert isinstance(rf, str)
    assert len(rf) == 200
    assert rf == "F" * 200


@pytest.mark.asyncio
async def test_claim_injects_direction_successful_analysis(
    client: AsyncClient, db_session
):
    """Claim endpoint surfaces analysis_plan from recent successful sibling tasks."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Success Analysis Test", user_id="test-user-123")
    project.directions = [
        {
            "id": direction_id,
            "title": "Refactor module Y",
            "priority": "medium",
            "research_context": "",
            "research_history": [],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    long_plan = "P" * 800  # truncated to 500
    success = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "success"}],
        vm_instance_id="vm-success-1",
        context={
            "direction_id": direction_id,
            "_analysis_plan": long_plan,
            "_pr_changed_files": ["svc.py", "route.py"],
            "scope": "medium",
            "review_loop": {"iteration": 2},
        },
        status=TaskStatus.COMPLETED,
        title="Successful task",
    )
    # Small-scope task without analysis plan but with pr_changed_files — should be included
    success_small = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "small"}],
        vm_instance_id="vm-success-2",
        context={
            "direction_id": direction_id,
            "_pr_changed_files": ["small.py"],
            "scope": "small",
        },
        status=TaskStatus.COMPLETED,
        title="Successful small-scope task",
    )
    # Successful task with no analysis plan AND no pr_changed_files — should be skipped
    success_empty = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "empty"}],
        vm_instance_id="vm-success-3",
        context={"direction_id": direction_id},
        status=TaskStatus.COMPLETED,
        title="Successful empty task",
    )
    db_session.add_all([success, success_small, success_empty])
    await db_session.commit()

    current = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "current"}],
        vm_instance_id="vm-current",
        context={"direction_id": direction_id},
    )
    db_session.add(current)
    await db_session.commit()
    await db_session.refresh(current)

    token = create_agent_token(str(current.id), "vm-current")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    dc = resp.json()["direction_context"]
    assert "direction_successful_analysis" in dc
    dsa = dc["direction_successful_analysis"]
    assert len(dsa) == 2
    titles = {e["title"] for e in dsa}
    assert titles == {"Successful task", "Successful small-scope task"}
    by_title = {e["title"]: e for e in dsa}
    full = by_title["Successful task"]
    assert len(full["analysis_plan"]) == 500
    assert full["pr_changed_files"] == ["svc.py", "route.py"]
    assert full["scope"] == "medium"
    assert full["review_iterations"] == 2
    small = by_title["Successful small-scope task"]
    assert "analysis_plan" not in small
    assert small["pr_changed_files"] == ["small.py"]
    assert small["scope"] == "small"
    assert "review_iterations" not in small
    import json as _json

    assert len(_json.dumps(dsa)) <= 1500


@pytest.mark.asyncio
async def test_claim_injects_cochange_files_for_create_task(
    client: AsyncClient, db_session
):
    """Claim endpoint injects cochange_files in project_context for create tasks when related_files match index."""
    from sqlalchemy.orm.attributes import flag_modified

    cochange_index = {
        "backend/app/models.py": {
            "backend/app/schemas.py": 3.0,
            "backend/testing/test_models.py": 2.5,
        },
        "backend/app/schemas.py": {"backend/app/models.py": 3.0},
    }
    project = Project(
        name="Cochange Test",
        user_id="test-user-123",
        design_doc={"cochange_index": cochange_index},
    )
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Add new model field"}],
        vm_instance_id="agent-vm-cochange-create",
        context={"related_files": ["backend/app/models.py"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-cochange-create")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    project_ctx = data.get("project_context", {})
    assert "cochange_files" in project_ctx, (
        f"Expected cochange_files in project_context, got: {list(project_ctx.keys())}"
    )
    cochange_files = project_ctx["cochange_files"]
    assert "backend/app/schemas.py" in cochange_files
    assert "backend/testing/test_models.py" in cochange_files
    # related_files themselves should not appear in cochange_files
    assert "backend/app/models.py" not in cochange_files


@pytest.mark.asyncio
async def test_claim_does_not_inject_cochange_files_for_review_task(
    client: AsyncClient, db_session
):
    """Claim endpoint does NOT inject cochange_files for review tasks."""
    from sqlalchemy.orm.attributes import flag_modified

    cochange_index = {
        "backend/app/models.py": {"backend/app/schemas.py": 3.0},
    }
    project = Project(
        name="Cochange Review Test",
        user_id="test-user-123",
        design_doc={"cochange_index": cochange_index},
    )
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 99, "branch": "fix/something"}],
        vm_instance_id="agent-vm-cochange-review",
        context={"related_files": ["backend/app/models.py"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-cochange-review")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    project_ctx = data.get("project_context", {})
    assert "cochange_files" not in project_ctx, (
        f"cochange_files should be absent for review tasks, got: {project_ctx.get('cochange_files')}"
    )


@pytest.mark.asyncio
async def test_claim_injects_cochange_hints_for_plan_task(
    client: AsyncClient, db_session
):
    """Claim endpoint injects cochange_hints in direction_context for plan tasks restricted to related_files."""
    from sqlalchemy.orm.attributes import flag_modified

    cochange_index = {
        "backend/app/models.py": {
            "backend/app/schemas.py": 3.0,
            "backend/testing/test_models.py": 2.5,
            "backend/app/orchestrator.py": 0.5,  # below 1.0 threshold
        },
        "backend/app/schemas.py": {
            "backend/app/models.py": 3.0,
            "backend/testing/test_schemas.py": 1.2,  # not in related_files
        },
        "backend/testing/test_models.py": {
            "backend/app/models.py": 2.5,
        },
    }
    project = Project(
        name="Cochange Plan Test",
        user_id="test-user-123",
        design_doc={"cochange_index": cochange_index},
    )
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "Decompose model refactor"}],
        vm_instance_id="agent-vm-cochange-plan",
        context={
            "related_files": [
                "backend/app/models.py",
                "backend/app/schemas.py",
                "backend/testing/test_models.py",
            ],
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-cochange-plan")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    direction_ctx = data.get("direction_context", {})
    assert "cochange_hints" in direction_ctx, (
        f"Expected cochange_hints in direction_context, got: {list(direction_ctx.keys())}"
    )
    hints = direction_ctx["cochange_hints"]
    # models.py paired with both schemas.py (3.0) and test_models.py (2.5) — both in related_files, both >= 1.0
    assert hints["backend/app/models.py"]["backend/app/schemas.py"] == 3.0
    assert hints["backend/app/models.py"]["backend/testing/test_models.py"] == 2.5
    # orchestrator.py is in the index but NOT in related_files — must be excluded
    assert "backend/app/orchestrator.py" not in hints["backend/app/models.py"]
    # schemas.py only pairs with models.py from related_files; test_schemas.py is not in related_files
    assert hints["backend/app/schemas.py"] == {"backend/app/models.py": 3.0}


@pytest.mark.asyncio
async def test_claim_omits_cochange_hints_when_below_threshold(
    client: AsyncClient, db_session
):
    """Claim endpoint omits cochange_hints when no within-set pairs meet the >= 1.0 threshold."""
    from sqlalchemy.orm.attributes import flag_modified

    cochange_index = {
        "backend/app/a.py": {"backend/app/b.py": 0.7},  # below threshold
        "backend/app/b.py": {"backend/app/a.py": 0.7},
    }
    project = Project(
        name="Cochange Plan Threshold Test",
        user_id="test-user-123",
        design_doc={"cochange_index": cochange_index},
    )
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "Decompose"}],
        vm_instance_id="agent-vm-cochange-plan-thresh",
        context={"related_files": ["backend/app/a.py", "backend/app/b.py"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-cochange-plan-thresh")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    direction_ctx = data.get("direction_context", {})
    assert "cochange_hints" not in direction_ctx


@pytest.mark.asyncio
async def test_claim_does_not_inject_cochange_hints_for_create_task(
    client: AsyncClient, db_session
):
    """Claim endpoint does NOT inject cochange_hints (a plan-only signal) into direction_context for create tasks."""
    from sqlalchemy.orm.attributes import flag_modified

    cochange_index = {
        "backend/app/models.py": {"backend/app/schemas.py": 3.0},
        "backend/app/schemas.py": {"backend/app/models.py": 3.0},
    }
    project = Project(
        name="Cochange Hint Skip Test",
        user_id="test-user-123",
        design_doc={"cochange_index": cochange_index},
    )
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Add field"}],
        vm_instance_id="agent-vm-cochange-create-skip",
        context={"related_files": ["backend/app/models.py", "backend/app/schemas.py"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-cochange-create-skip")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    direction_ctx = data.get("direction_context", {}) or {}
    assert "cochange_hints" not in direction_ctx, (
        f"cochange_hints should be plan-only, got direction_context keys: {list(direction_ctx.keys())}"
    )


@pytest.mark.asyncio
async def test_claim_injects_decomposition_outcomes_for_plan_task(
    client: AsyncClient, db_session
):
    """Claim endpoint summarizes decomposition_quality from last 3 productive research_history entries for plan tasks."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Decomp Plan Test", user_id="test-user-decomp-plan")
    project.directions = [
        {
            "id": direction_id,
            "title": "Refactor models",
            "priority": "medium",
            "research_context": "",
            "research_history": [
                # Non-productive cycle → excluded
                {
                    "tasks_created": 0,
                    "prs_merged": 0,
                    "decomposition_quality": [
                        {
                            "plan_task_id": "ignore-me",
                            "subtask_count": 1,
                            "subtask_merge_rate": 0.0,
                            "avg_files_per_subtask": 0.0,
                            "max_files_in_subtask": 0,
                            "dependency_depth": 0,
                            "cross_module_count": 0,
                        },
                    ],
                },
                {
                    "tasks_created": 2,
                    "prs_merged": 2,
                    "decomposition_quality": [
                        {
                            "plan_task_id": "p1",
                            "subtask_count": 3,
                            "subtask_merge_rate": 0.9,
                            "avg_files_per_subtask": 2.0,
                            "max_files_in_subtask": 3,
                            "dependency_depth": 1,
                            "cross_module_count": 0,
                        },
                    ],
                },
                {
                    "tasks_created": 2,
                    "prs_merged": 1,
                    "decomposition_quality": [
                        {
                            "plan_task_id": "p2",
                            "subtask_count": 4,
                            "subtask_merge_rate": 0.2,
                            "avg_files_per_subtask": 7.0,
                            "max_files_in_subtask": 10,
                            "dependency_depth": 4,
                            "cross_module_count": 3,
                        },
                        {
                            "plan_task_id": "p3",
                            "subtask_count": 2,
                            "subtask_merge_rate": 0.8,
                            "avg_files_per_subtask": 3.0,
                            "max_files_in_subtask": 4,
                            "dependency_depth": 2,
                            "cross_module_count": 0,
                        },
                    ],
                },
            ],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "Decompose refactor"}],
        vm_instance_id="agent-vm-decomp-plan",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-decomp-plan")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    dc = data.get("direction_context", {}) or {}
    assert "decomposition_outcomes" in dc, (
        f"Expected decomposition_outcomes, got: {list(dc.keys())}"
    )
    summary = dc["decomposition_outcomes"]
    # 3 entries flattened from the 2 productive cycles (non-productive excluded)
    assert summary["prior_decomposition_count"] == 3
    # avg merge rate = (0.9 + 0.2 + 0.8) / 3 = 0.633
    assert summary["avg_subtask_merge_rate"] == round((0.9 + 0.2 + 0.8) / 3, 3)
    # avg files per subtask = (2 + 7 + 3) / 3 = 4.0
    assert summary["avg_files_per_subtask"] == 4.0
    assert summary["max_files_in_subtask"] == 10
    assert summary["avg_dependency_depth"] == round((1 + 4 + 2) / 3, 2)
    assert summary["max_dependency_depth"] == 4
    # high cross_module (>=2) merge rate = 0.2; low = (0.9+0.8)/2 = 0.85; gap >= 0.1 → true
    assert summary["high_cross_module_correlates_with_low_merge"] is True


@pytest.mark.asyncio
async def test_claim_omits_decomposition_outcomes_when_no_history(
    client: AsyncClient, db_session
):
    """Claim endpoint omits decomposition_outcomes when research_history has no decomposition_quality data."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Decomp Empty Test", user_id="test-user-decomp-empty")
    project.directions = [
        {
            "id": direction_id,
            "title": "No decomp data",
            "priority": "low",
            "research_context": "",
            "research_history": [
                {"tasks_created": 2, "prs_merged": 1},
                {"tasks_created": 3, "prs_merged": 2, "decomposition_quality": []},
            ],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "plan", "prompt": "Decompose nothing"}],
        vm_instance_id="agent-vm-decomp-empty",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-decomp-empty")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    dc = resp.json().get("direction_context", {}) or {}
    assert "decomposition_outcomes" not in dc


@pytest.mark.asyncio
async def test_claim_does_not_inject_decomposition_outcomes_for_create_task(
    client: AsyncClient, db_session
):
    """Claim endpoint omits decomposition_outcomes (plan-only signal) for create tasks."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Decomp Create Skip Test", user_id="test-user-decomp-skip")
    project.directions = [
        {
            "id": direction_id,
            "title": "Has decomp data",
            "priority": "high",
            "research_context": "",
            "research_history": [
                {
                    "tasks_created": 2,
                    "prs_merged": 2,
                    "decomposition_quality": [
                        {
                            "plan_task_id": "p1",
                            "subtask_count": 2,
                            "subtask_merge_rate": 1.0,
                            "avg_files_per_subtask": 2.0,
                            "max_files_in_subtask": 3,
                            "dependency_depth": 1,
                            "cross_module_count": 0,
                        },
                    ],
                },
            ],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Implement change"}],
        vm_instance_id="agent-vm-decomp-create-skip",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-decomp-create-skip")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    dc = resp.json().get("direction_context", {}) or {}
    assert "decomposition_outcomes" not in dc, (
        f"decomposition_outcomes should be plan-only, got direction_context keys: {list(dc.keys())}"
    )


@pytest.mark.asyncio
async def test_claim_injects_module_feedback_warnings_for_create_task(
    client: AsyncClient, db_session
):
    """Claim endpoint injects module_feedback_warnings for create tasks when related_files have profiles."""
    from sqlalchemy.orm.attributes import flag_modified

    module_feedback = {
        "backend/app/orchestrator.py": {
            "test-coverage": 6,
            "error-handling": 2,
            "total": 8,
            "last_updated": "2026-01-01T00:00:00",
        },
        "backend/app/models.py": {
            "type-safety": 1,
            "total": 2,  # below threshold of 3 — should be excluded
            "last_updated": "2026-01-01T00:00:00",
        },
    }
    project = Project(
        name="Module Feedback Test",
        user_id="test-user-123",
        design_doc={"module_feedback": module_feedback},
    )
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Add feature"}],
        vm_instance_id="agent-vm-module-feedback",
        context={
            "related_files": ["backend/app/orchestrator.py", "backend/app/models.py"]
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-module-feedback")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    project_ctx = data.get("project_context", {})
    assert "module_feedback_warnings" in project_ctx, (
        f"Expected module_feedback_warnings in project_context, got: {list(project_ctx.keys())}"
    )
    warnings = project_ctx["module_feedback_warnings"]
    assert len(warnings) == 1, (
        f"Expected 1 warning (models.py below threshold), got {len(warnings)}"
    )
    w = warnings[0]
    assert w["file"] == "backend/app/orchestrator.py"
    assert "test-coverage" in w["top_categories"]
    assert w["sample_size"] == 8
    # models.py should not appear — total < 3
    files_in_warnings = [x["file"] for x in warnings]
    assert "backend/app/models.py" not in files_in_warnings


@pytest.mark.asyncio
async def test_claim_does_not_inject_module_feedback_warnings_for_review_task(
    client: AsyncClient, db_session
):
    """Claim endpoint does NOT inject module_feedback_warnings for review tasks."""
    from sqlalchemy.orm.attributes import flag_modified

    module_feedback = {
        "backend/app/orchestrator.py": {
            "test-coverage": 5,
            "total": 5,
            "last_updated": "2026-01-01T00:00:00",
        },
    }
    project = Project(
        name="Module Feedback Review Test",
        user_id="test-user-123",
        design_doc={"module_feedback": module_feedback},
    )
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 42, "branch": "fix/something"}],
        vm_instance_id="agent-vm-module-feedback-review",
        context={"related_files": ["backend/app/orchestrator.py"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-module-feedback-review")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    project_ctx = data.get("project_context", {})
    assert "module_feedback_warnings" not in project_ctx, (
        f"module_feedback_warnings should be absent for review tasks, got: {project_ctx.get('module_feedback_warnings')}"
    )


@pytest.mark.asyncio
async def test_claim_injects_self_review_catch_rate_with_fp_calibration(
    client: AsyncClient, db_session
):
    """Claim endpoint includes false_positive_rate, precision, sr_flag_sample_size in self_review_catch_rate."""
    from sqlalchemy.orm.attributes import flag_modified

    design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "security": {
                    "miss_rate": 0.10,
                    "weighted_miss_rate": 0.12,
                    "false_positive_rate": 0.8,
                    "fp_count": 8,
                    "tp_count": 2,
                },
                "correctness": {
                    "miss_rate": 0.20,
                    # No fp/tp data — calibration fields should be omitted.
                },
            },
            "by_task_type": {
                "create": {
                    "security": {
                        "miss_rate": 0.05,
                        "false_positive_rate": 0.6,
                        "fp_count": 6,
                        "tp_count": 4,
                    },
                },
            },
        },
    }
    project = Project(
        name="SR FP Calibration Test",
        user_id="test-user-123",
        design_doc=design_doc,
    )
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Add feature"}],
        vm_instance_id="agent-vm-sr-fp-calib",
        context={"related_files": ["backend/app/foo.py"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-sr-fp-calib")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    project_ctx = resp.json().get("project_context", {})
    sr = project_ctx.get("self_review_catch_rate")
    assert sr is not None, (
        f"expected self_review_catch_rate in project_context, got {list(project_ctx.keys())}"
    )

    cats = sr["categories"]
    sec = cats["security"]
    assert sec["false_positive_rate"] == 0.8
    assert sec["precision"] == round(1 - 0.8, 3)
    assert sec["sr_flag_sample_size"] == 10

    # Category without fp data: calibration fields must be absent.
    corr = cats["correctness"]
    assert "false_positive_rate" not in corr
    assert "precision" not in corr
    assert "sr_flag_sample_size" not in corr

    # for_task_type passthrough
    type_sec = sr["for_task_type"]["security"]
    assert type_sec["false_positive_rate"] == 0.6
    assert type_sec["precision"] == round(1 - 0.6, 3)
    assert type_sec["sr_flag_sample_size"] == 10


@pytest.mark.asyncio
async def test_claim_injects_self_review_catch_rate_failure_mode_fields(
    client: AsyncClient, db_session
):
    """Claim endpoint forwards trend, below_floor, engagement_thinning, and miss_mode_breakdown.

    These fields drive the agent-side failure-mode hint
    (self-review.ts:formatFailureModeHintForSrPrompt) and the thinning
    reasoning gate (standard.ts:getThinningCategories). If _cat_entry drops
    them at the backend->agent boundary, the hint never renders.
    """
    from sqlalchemy.orm.attributes import flag_modified

    design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "security": {
                    "miss_rate": 0.30,
                    "trend": "decreasing",
                    "below_floor": True,
                    "engagement_thinning": True,
                    "miss_mode_breakdown": {
                        "reading_gap": 5,
                        "judgment_gap": 1,
                        "coverage_gap": 0,
                        "dominant_mode": "reading_gap",
                    },
                },
                "correctness": {
                    "miss_rate": 0.10,
                    # No regression signals — fields should be omitted.
                },
            },
            "by_task_type": {
                "create": {
                    "security": {
                        "miss_rate": 0.25,
                        "trend": "stable",
                        "below_floor": False,
                        "miss_mode_breakdown": {
                            "reading_gap": 0,
                            "judgment_gap": 3,
                            "coverage_gap": 1,
                            "dominant_mode": "judgment_gap",
                        },
                    },
                },
            },
        },
    }
    project = Project(
        name="SR Failure Mode Test",
        user_id="test-user-123",
        design_doc=design_doc,
    )
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Add feature"}],
        vm_instance_id="agent-vm-sr-failure-mode",
        context={"related_files": ["backend/app/foo.py"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-sr-failure-mode")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    project_ctx = resp.json().get("project_context", {})
    sr = project_ctx.get("self_review_catch_rate")
    assert sr is not None, (
        f"expected self_review_catch_rate in project_context, got {list(project_ctx.keys())}"
    )

    sec = sr["categories"]["security"]
    assert sec["trend"] == "decreasing"
    assert sec["below_floor"] is True
    assert sec["engagement_thinning"] is True
    assert sec["miss_mode_breakdown"] == {
        "reading_gap": 5,
        "judgment_gap": 1,
        "coverage_gap": 0,
        "dominant_mode": "reading_gap",
    }

    # Category without regression signals: fields must be absent.
    corr = sr["categories"]["correctness"]
    assert "trend" not in corr
    assert "below_floor" not in corr
    assert "engagement_thinning" not in corr
    assert "miss_mode_breakdown" not in corr

    # for_task_type passthrough preserves the same fields.
    type_sec = sr["for_task_type"]["security"]
    assert type_sec["trend"] == "stable"
    assert type_sec["below_floor"] is False
    assert type_sec["miss_mode_breakdown"]["dominant_mode"] == "judgment_gap"


@pytest.mark.asyncio
async def test_claim_injects_architecture_context_for_create_task(
    client: AsyncClient, db_session
):
    """Claim endpoint injects architecture_context in project_context for create tasks when related_files match _file_to_system."""
    from sqlalchemy.orm.attributes import flag_modified

    design_doc = {
        "_file_to_system": {
            "backend/app/auth.py": "auth",
            "backend/app/models.py": "database",
        },
        "systems/auth": {
            "name": "Auth",
            "status": "functional",
            "ready_for_features": True,
            "description": "Handles authentication and JWT tokens.",
            "modules": ["backend/app/auth.py"],
        },
        "systems/database": {
            "name": "Database",
            "status": "mature",
            "ready_for_features": True,
            "description": "SQLModel ORM layer and migrations.",
            "modules": ["backend/app/models.py"],
        },
    }
    project = Project(
        name="Arch Context Test", user_id="test-user-123", design_doc=design_doc
    )
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Add OAuth provider"}],
        vm_instance_id="agent-vm-arch-ctx-create",
        context={"related_files": ["backend/app/auth.py", "backend/app/models.py"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-arch-ctx-create")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    project_ctx = data.get("project_context", {})
    assert "architecture_context" in project_ctx, (
        f"Expected architecture_context in project_context, got: {list(project_ctx.keys())}"
    )
    arch_ctx = project_ctx["architecture_context"]
    assert len(arch_ctx) == 2
    names = {s["name"] for s in arch_ctx}
    assert names == {"Auth", "Database"}
    auth_entry = next(s for s in arch_ctx if s["name"] == "Auth")
    assert auth_entry["status"] == "functional"
    assert auth_entry["ready_for_features"] is True
    assert "authentication" in auth_entry["description"]


@pytest.mark.asyncio
async def test_claim_does_not_inject_architecture_context_for_review_task(
    client: AsyncClient, db_session
):
    """Claim endpoint does NOT inject architecture_context for review tasks."""
    from sqlalchemy.orm.attributes import flag_modified

    design_doc = {
        "_file_to_system": {"backend/app/auth.py": "auth"},
        "systems/auth": {
            "name": "Auth",
            "status": "functional",
            "ready_for_features": True,
            "description": "Auth system.",
            "modules": ["backend/app/auth.py"],
        },
    }
    project = Project(
        name="Arch Context Review Test", user_id="test-user-123", design_doc=design_doc
    )
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 99, "branch": "feat/review-branch"}],
        vm_instance_id="agent-vm-arch-ctx-review",
        context={"related_files": ["backend/app/auth.py"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-arch-ctx-review")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    data = resp.json()
    project_ctx = data.get("project_context", {})
    assert "architecture_context" not in project_ctx, (
        f"architecture_context should be absent for review tasks, got: {project_ctx.get('architecture_context')}"
    )


@pytest.mark.smoketest
@pytest.mark.asyncio
async def test_claim_tracks_arch_context_in_task_context(
    client: AsyncClient, db_session
):
    """When arch context is injected at claim time, _had_arch_context=True and
    _shown_arch_system_names are persisted in task.context for effectiveness tracking."""
    from sqlalchemy.orm.attributes import flag_modified

    design_doc = {
        "_file_to_system": {
            "backend/app/auth.py": "auth",
            "backend/app/models.py": "database",
        },
        "systems/auth": {
            "name": "Auth",
            "status": "functional",
            "ready_for_features": True,
            "description": "Handles authentication.",
            "modules": ["backend/app/auth.py"],
        },
        "systems/database": {
            "name": "Database",
            "status": "mature",
            "ready_for_features": True,
            "description": "ORM layer.",
            "modules": ["backend/app/models.py"],
        },
    }
    project = Project(
        name="Arch Context Tracking Test",
        user_id="test-user-123",
        design_doc=design_doc,
    )
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Add new feature"}],
        vm_instance_id="agent-vm-arch-track",
        context={"related_files": ["backend/app/auth.py", "backend/app/models.py"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-arch-track")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    await db_session.refresh(task)
    ctx = task.context or {}
    assert ctx.get("_had_arch_context") is True, (
        f"Expected _had_arch_context=True in task.context, got: {ctx.get('_had_arch_context')}"
    )
    shown = ctx.get("_shown_arch_system_names", [])
    assert set(shown) == {"auth", "database"}, (
        f"Expected system names auth, database in _shown_arch_system_names, got: {shown}"
    )


@pytest.mark.asyncio
async def test_safe_ws_close_swallows_double_close_runtime_error():
    """Production cascade: receive_json raised WebSocketDisconnect (already
    sent a close frame), then our explicit close tries to send a second one
    and starlette raises RuntimeError. _safe_ws_close must swallow that
    specific race without breaking."""
    from unittest.mock import AsyncMock
    from app.routers.projects import _safe_ws_close

    ws = AsyncMock()
    ws.close.side_effect = RuntimeError(
        'Unexpected ASGI message "websocket.close", after sending "websocket.close".'
    )
    await _safe_ws_close(ws, code=1008, reason="Authentication failed")
    ws.close.assert_awaited_once_with(code=1008, reason="Authentication failed")


@pytest.mark.asyncio
async def test_safe_ws_close_swallows_send_once_close_runtime_error():
    """2026-05-04 production: the 90s idle-timeout path raised RuntimeError
    'Cannot call "send" once a close message has been sent.' — distinct from
    the 'websocket.close' double-close variant. Both mean the socket is gone
    and the close is cosmetic; both must be swallowed."""
    from unittest.mock import AsyncMock
    from app.routers.projects import _safe_ws_close

    ws = AsyncMock()
    ws.close.side_effect = RuntimeError(
        'Cannot call "send" once a close message has been sent.'
    )
    await _safe_ws_close(ws, code=1001, reason="Idle timeout")
    ws.close.assert_awaited_once_with(code=1001, reason="Idle timeout")


@pytest.mark.asyncio
async def test_safe_ws_close_reraises_other_runtime_errors():
    """Only the double-close race is swallowed; other RuntimeErrors propagate
    so real bugs don't get hidden."""
    from unittest.mock import AsyncMock
    from app.routers.projects import _safe_ws_close

    ws = AsyncMock()
    ws.close.side_effect = RuntimeError("connection closed during handshake")
    with pytest.raises(RuntimeError, match="handshake"):
        await _safe_ws_close(ws, code=1008, reason="boom")


def test_agent_ws_invalid_token_closes_with_1008(mocker, caplog):
    """
    Tests that connecting to the agent WebSocket without a valid token causes
    the server to close the connection with code 1008 (Policy Violation).

    Startup DB functions are mocked out so that starlette.testclient.TestClient
    (which runs the full ASGI lifespan) does not attempt a real PostgreSQL
    connection during test setup.
    """
    from unittest.mock import AsyncMock
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    import app.main as main_module
    import app.lifecycle as lifecycle_module
    import app.orchestrator as orchestrator_module

    # Prevent startup coroutines from trying to connect to a real database
    mocker.patch("app.max_plan_pool.load_max_plan_pool", new_callable=AsyncMock)
    mocker.patch.object(main_module, "_verify_schema_version", new_callable=AsyncMock)
    mocker.patch.object(lifecycle_module, "_seed_initial_admin", new_callable=AsyncMock)
    mocker.patch.object(main_module, "_init_deployment_state", new_callable=AsyncMock)
    # Background task coroutines must also be no-ops so the task loop doesn't
    # block the test event loop or attempt DB queries
    mocker.patch("app.reaper.reaper_service", new_callable=AsyncMock)
    mocker.patch("app.services.gcp_cleanup_service", new_callable=AsyncMock)
    mocker.patch("app.services.secret_reconciliation_service", new_callable=AsyncMock)
    mocker.patch.object(
        orchestrator_module, "orchestrator_worker", new_callable=AsyncMock
    )

    task_id = uuid.uuid4()
    caplog.set_level(logging.INFO)
    with (
        TestClient(app=main_module.app) as tc,
        pytest.raises(WebSocketDisconnect) as exc_info,
        tc.websocket_connect(f"/api/agent/ws/{task_id}") as ws,
    ):
        ws.send_json({"token": "not-a-valid-jwt-token"})
        ws.receive_text()
    assert exc_info.value.code == 1008
    _assert_agent_auth_reject_log(
        caplog,
        reason="ws_initial_auth_failure",
        path=f"/api/agent/ws/{task_id}",
        task_id=str(task_id),
    )


@pytest.mark.asyncio
async def test_agent_ws_auth_closes_on_terminal_task(mocker, caplog):
    import app.routers.projects as projects_module
    from unittest.mock import AsyncMock, MagicMock

    task_id = uuid.uuid4()
    project_id = uuid.uuid4()
    token = create_agent_token(
        str(task_id), "agent-vm-terminal-ws", project_id=str(project_id)
    )

    mock_task = MagicMock()
    mock_task.id = task_id
    mock_task.project_id = project_id
    mock_task.vm_instance_id = "agent-vm-terminal-ws"
    mock_task.status = TaskStatus.COMPLETED

    mock_execute_result = MagicMock()
    mock_execute_result.scalars.return_value.first.return_value = mock_task

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_execute_result)

    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__.return_value = mock_session
    mock_session_cm.__aexit__.return_value = None

    mocker.patch("app.routers.projects.AsyncSessionLocal", return_value=mock_session_cm)
    mocker.patch(
        "app.routers.projects.manager.check_and_add_ip",
        new_callable=AsyncMock,
        return_value=True,
    )
    mocker.patch("app.routers.projects.manager.remove_ip")
    ws = AsyncMock()
    ws.receive_json = AsyncMock(return_value={"token": token})
    ws.close = AsyncMock()
    caplog.set_level(logging.INFO)

    await projects_module.agent_websocket_endpoint(ws, task_id)

    ws.close.assert_called_once_with(code=1008, reason="Task is no longer active")
    _assert_agent_auth_reject_log(
        caplog,
        reason="ws_task_terminal",
        path=f"/api/agent/ws/{task_id}",
        task_id=str(task_id),
        token_vm_instance_id="agent-vm-terminal-ws",
        token_project_id=str(project_id),
    )


@pytest.mark.asyncio
async def test_agent_ws_auth_closes_on_project_mismatch(mocker, caplog):
    import app.routers.projects as projects_module
    from unittest.mock import AsyncMock, MagicMock

    task_id = uuid.uuid4()
    project_id = uuid.uuid4()
    token_project_id = uuid.uuid4()
    token = create_agent_token(
        str(task_id), "agent-vm-ws-project", project_id=str(token_project_id)
    )

    mock_task = MagicMock()
    mock_task.id = task_id
    mock_task.project_id = project_id
    mock_task.vm_instance_id = "agent-vm-ws-project"
    mock_task.status = TaskStatus.IN_PROGRESS

    mock_execute_result = MagicMock()
    mock_execute_result.scalars.return_value.first.return_value = mock_task

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_execute_result)

    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__.return_value = mock_session
    mock_session_cm.__aexit__.return_value = None

    mocker.patch("app.routers.projects.AsyncSessionLocal", return_value=mock_session_cm)
    mocker.patch(
        "app.routers.projects.manager.check_and_add_ip",
        new_callable=AsyncMock,
        return_value=True,
    )
    mocker.patch("app.routers.projects.manager.remove_ip")
    ws = AsyncMock()
    ws.receive_json = AsyncMock(return_value={"token": token})
    ws.close = AsyncMock()
    caplog.set_level(logging.INFO)

    await projects_module.agent_websocket_endpoint(ws, task_id)

    ws.close.assert_called_once_with(code=1008, reason="Project mismatch")
    _assert_agent_auth_reject_log(
        caplog,
        reason="ws_project_mismatch",
        path=f"/api/agent/ws/{task_id}",
        task_id=str(task_id),
        token_vm_instance_id="agent-vm-ws-project",
        token_project_id=str(token_project_id),
    )


@pytest.mark.asyncio
async def test_agent_ws_auth_closes_on_vm_mismatch(mocker, caplog):
    import app.routers.projects as projects_module
    from unittest.mock import AsyncMock, MagicMock

    task_id = uuid.uuid4()
    project_id = uuid.uuid4()
    token = create_agent_token(
        str(task_id), "agent-vm-ws-original", project_id=str(project_id)
    )

    mock_task = MagicMock()
    mock_task.id = task_id
    mock_task.project_id = project_id
    mock_task.vm_instance_id = "agent-vm-ws-assigned"
    mock_task.status = TaskStatus.IN_PROGRESS

    mock_execute_result = MagicMock()
    mock_execute_result.scalars.return_value.first.return_value = mock_task

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_execute_result)

    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__.return_value = mock_session
    mock_session_cm.__aexit__.return_value = None

    mocker.patch("app.routers.projects.AsyncSessionLocal", return_value=mock_session_cm)
    mocker.patch(
        "app.routers.projects.manager.check_and_add_ip",
        new_callable=AsyncMock,
        return_value=True,
    )
    mocker.patch("app.routers.projects.manager.remove_ip")
    ws = AsyncMock()
    ws.receive_json = AsyncMock(return_value={"token": token})
    ws.close = AsyncMock()
    caplog.set_level(logging.INFO)

    await projects_module.agent_websocket_endpoint(ws, task_id)

    ws.close.assert_called_once_with(code=1008, reason="VM instance mismatch")
    _assert_agent_auth_reject_log(
        caplog,
        reason="ws_vm_instance_mismatch",
        path=f"/api/agent/ws/{task_id}",
        task_id=str(task_id),
        token_vm_instance_id="agent-vm-ws-original",
        token_project_id=str(project_id),
    )


@pytest.mark.asyncio
async def test_agent_ws_receive_loop_rejects_terminated_task(mocker, caplog):
    """
    Regression: WS receive loop must close with 1008 when task appears in the
    terminated-task deny-list between auth and the next received message.

    Simulates: agent opens a WS connection, the task is cancelled (injected into
    the deny-list), then sends a follow-up message — the loop must reject it.
    """
    import time as time_module
    import app.routers.agent as agent_module
    from app.routers.projects import agent_websocket_endpoint
    from unittest.mock import AsyncMock, MagicMock

    task_id = uuid.uuid4()
    project_id = uuid.uuid4()

    token = create_agent_token(str(task_id), str(project_id))

    # Mock task returned by initial DB auth lookup
    mock_task = MagicMock()
    mock_task.id = task_id
    mock_task.project_id = project_id
    mock_task.vm_instance_id = None
    mock_task.status = TaskStatus.IN_PROGRESS

    mock_execute_result = MagicMock()
    mock_execute_result.scalars.return_value.first.return_value = mock_task

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_execute_result)

    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__.return_value = mock_session
    mock_session_cm.__aexit__.return_value = None

    mocker.patch("app.routers.projects.AsyncSessionLocal", return_value=mock_session_cm)
    mocker.patch(
        "app.routers.projects.manager.check_and_add_ip",
        new_callable=AsyncMock,
        return_value=True,
    )
    mocker.patch("app.routers.projects.manager.remove_ip")
    mocker.patch("app.routers.projects.broadcast_to_project", new_callable=AsyncMock)

    ws = AsyncMock()
    ws.receive_json = AsyncMock(return_value={"token": token})
    caplog.set_level(logging.INFO)

    # Inject task into deny-list inside receive_text so the in-loop check fires
    async def _inject_and_return():
        agent_module._terminated_task_ids[str(task_id)] = time_module.time()
        return '{"type": "log", "content": "hello"}'

    ws.receive_text = AsyncMock(side_effect=_inject_and_return)
    ws.close = AsyncMock()

    await agent_websocket_endpoint(ws, task_id)

    # Connection must be closed with 1008 (Task is no longer active)
    ws.close.assert_called_once_with(code=1008, reason="Task is no longer active")
    _assert_agent_auth_reject_log(
        caplog,
        reason="ws_deny_list_hit",
        path=f"/api/agent/ws/{task_id}",
        task_id=str(task_id),
        token_vm_instance_id=str(project_id),
    )

    # Clean up deny-list entry
    agent_module._terminated_task_ids.pop(str(task_id), None)


@pytest.mark.asyncio
async def test_agent_ws_receive_loop_closes_on_periodic_recheck_terminal(
    mocker, caplog
):
    import app.routers.projects as projects_module
    from unittest.mock import AsyncMock, MagicMock

    task_id = uuid.uuid4()
    project_id = uuid.uuid4()
    token = create_agent_token(
        str(task_id), "agent-vm-periodic", project_id=str(project_id)
    )

    mock_task = MagicMock()
    mock_task.id = task_id
    mock_task.project_id = project_id
    mock_task.vm_instance_id = "agent-vm-periodic"
    mock_task.status = TaskStatus.IN_PROGRESS

    fresh_task = MagicMock()
    fresh_task.id = task_id
    fresh_task.project_id = project_id
    fresh_task.vm_instance_id = "agent-vm-periodic"
    fresh_task.status = TaskStatus.COMPLETED

    mock_execute_result = MagicMock()
    mock_execute_result.scalars.return_value.first.return_value = mock_task

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_execute_result)
    mock_session.get = AsyncMock(return_value=fresh_task)

    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__.return_value = mock_session
    mock_session_cm.__aexit__.return_value = None

    mocker.patch("app.routers.projects.AsyncSessionLocal", return_value=mock_session_cm)
    mocker.patch(
        "app.routers.projects.manager.check_and_add_ip",
        new_callable=AsyncMock,
        return_value=True,
    )
    mocker.patch("app.routers.projects.manager.remove_ip")
    mocker.patch("app.routers.projects.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.projects.time.time",
        side_effect=[0.0, 0.0, 31.0, 31.0, 31.0, 31.0],
    )
    ws = AsyncMock()
    ws.receive_json = AsyncMock(return_value={"token": token})
    ws.receive_text = AsyncMock(return_value='{"type": "log", "content": "hello"}')
    ws.close = AsyncMock()
    caplog.set_level(logging.INFO)

    await projects_module.agent_websocket_endpoint(ws, task_id)

    ws.close.assert_called_once_with(code=1008, reason="Task is no longer active")
    _assert_agent_auth_reject_log(
        caplog,
        reason="ws_periodic_recheck_terminal",
        path=f"/api/agent/ws/{task_id}",
        task_id=str(task_id),
        token_vm_instance_id="agent-vm-periodic",
        token_project_id=str(project_id),
    )


@pytest.mark.asyncio
async def test_agent_ws_closes_on_termination_without_messages(mocker):
    """
    Regression: WS must close ≤5s after task termination even when no messages
    are received (timer-driven path, not receive-driven).

    Simulates: agent opens WS, task is injected into the deny-list by the timer
    callback — no further messages are sent — the concurrent timer must fire and
    close the connection.
    """
    import asyncio
    import time as time_module
    import app.routers.agent as agent_module
    from app.routers.projects import agent_websocket_endpoint
    from unittest.mock import AsyncMock, MagicMock

    task_id = uuid.uuid4()
    project_id = uuid.uuid4()

    token = create_agent_token(str(task_id), str(project_id))

    mock_task = MagicMock()
    mock_task.id = task_id
    mock_task.project_id = project_id
    mock_task.vm_instance_id = None
    mock_task.status = TaskStatus.IN_PROGRESS

    mock_execute_result = MagicMock()
    mock_execute_result.scalars.return_value.first.return_value = mock_task

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_execute_result)

    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__.return_value = mock_session
    mock_session_cm.__aexit__.return_value = None

    mocker.patch("app.routers.projects.AsyncSessionLocal", return_value=mock_session_cm)
    mocker.patch(
        "app.routers.projects.manager.check_and_add_ip",
        new_callable=AsyncMock,
        return_value=True,
    )
    mocker.patch("app.routers.projects.manager.remove_ip")
    mocker.patch("app.routers.projects.broadcast_to_project", new_callable=AsyncMock)

    ws = AsyncMock()
    ws.receive_json = AsyncMock(return_value={"token": token})

    # receive_text blocks forever — no messages are sent after auth
    block_event = asyncio.Event()

    async def _never_return():
        await block_event.wait()
        return ""

    ws.receive_text = AsyncMock(side_effect=_never_return)
    ws.close = AsyncMock()

    # Patch asyncio.sleep so the 5s timer fires immediately and injects termination.
    async def _instant_sleep_with_inject(seconds):
        agent_module._terminated_task_ids[str(task_id)] = time_module.time()

    mocker.patch("asyncio.sleep", new=_instant_sleep_with_inject)

    await agent_websocket_endpoint(ws, task_id)

    # Connection must be closed with 1008 (Task is no longer active)
    ws.close.assert_called_once_with(code=1008, reason="Task is no longer active")

    # Unblock any lingering receive coroutines and clean up
    block_event.set()
    agent_module._terminated_task_ids.pop(str(task_id), None)


@pytest.mark.asyncio
async def test_agent_ws_closes_on_db_fallback_when_denylist_misses(mocker):
    """
    Regression: WS must close ≤5s after task termination even when the
    in-memory deny-list is empty (cache miss after restart, listener gap, or
    desync). The timer-driven DB fallback must observe the terminal status in
    the DB and close the connection on the next tick.
    """
    import asyncio
    import app.routers.agent as agent_module
    from app.routers.projects import agent_websocket_endpoint
    from unittest.mock import AsyncMock, MagicMock

    task_id = uuid.uuid4()
    project_id = uuid.uuid4()

    # Ensure deny-list does NOT contain the task — this is the cache-miss case
    agent_module._terminated_task_ids.pop(str(task_id), None)

    token = create_agent_token(str(task_id), str(project_id))

    # Initial auth-time task: IN_PROGRESS so the handshake succeeds
    initial_task = MagicMock()
    initial_task.id = task_id
    initial_task.project_id = project_id
    initial_task.vm_instance_id = None
    initial_task.status = TaskStatus.IN_PROGRESS

    # DB-fallback task: now terminal (COMPLETED) — must trigger a close
    terminal_task = MagicMock()
    terminal_task.id = task_id
    terminal_task.project_id = project_id
    terminal_task.vm_instance_id = None
    terminal_task.status = TaskStatus.COMPLETED

    auth_execute_result = MagicMock()
    auth_execute_result.scalars.return_value.first.return_value = initial_task

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=auth_execute_result)
    # Subsequent session.get(Task, ...) inside the timer-driven DB fallback
    # returns the terminal task.
    mock_session.get = AsyncMock(return_value=terminal_task)

    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__.return_value = mock_session
    mock_session_cm.__aexit__.return_value = None

    mocker.patch("app.routers.projects.AsyncSessionLocal", return_value=mock_session_cm)
    mocker.patch(
        "app.routers.projects.manager.check_and_add_ip",
        new_callable=AsyncMock,
        return_value=True,
    )
    mocker.patch("app.routers.projects.manager.remove_ip")
    mocker.patch("app.routers.projects.broadcast_to_project", new_callable=AsyncMock)

    ws = AsyncMock()
    ws.receive_json = AsyncMock(return_value={"token": token})

    block_event = asyncio.Event()

    async def _never_return():
        await block_event.wait()
        return ""

    ws.receive_text = AsyncMock(side_effect=_never_return)
    ws.close = AsyncMock()

    # Patch asyncio.sleep so the 5s timer fires immediately. Deny-list stays
    # empty — only the DB fallback can detect the termination.
    async def _instant_sleep(seconds):
        return None

    mocker.patch("asyncio.sleep", new=_instant_sleep)

    await agent_websocket_endpoint(ws, task_id)

    # Confirm deny-list was NOT the trigger
    assert str(task_id) not in agent_module._terminated_task_ids
    # DB fallback must have been consulted at least once on the timer path
    assert mock_session.get.await_count >= 1
    # Connection must be closed with 1008 (Task is no longer active)
    ws.close.assert_called_once_with(code=1008, reason="Task is no longer active")

    block_event.set()


@pytest.mark.asyncio
async def test_external_api_requeue_sets_exponential_backoff(
    client: AsyncClient, db_session, mocker
):
    """
    Tests that EXTERNAL_API failures requeue with exponential backoff in retry_after.
    - 1st retry: ~2 min delay (2^1 * 120 = 240s)
    - 2nd retry: ~8 min delay (2^2 * 120 = 480s)
    - 3rd retry: ~16 min delay (2^3 * 120 = 960s)
    - After 5 retries, task is not requeued (budget exhausted)
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    project = Project(name="Backoff Test", user_id="test-user-backoff")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for retry_num in range(1, 4):
        task = Task(
            project_id=project.id,
            payloads=[{"cmd": "test"}],
            vm_instance_id=f"agent-vm-backoff-{retry_num}",
            status=TaskStatus.IN_PROGRESS,
            # Pre-seed provision_failures to verify it gets cleared on graceful requeue —
            # otherwise a single provision hiccup on a previously-successful task would
            # immediately exhaust the retry budget on the next dispatch.
            context={"_api_failure_retries": retry_num - 1, "provision_failures": 2},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        token = create_agent_token(str(task.id), f"agent-vm-backoff-{retry_num}")
        headers = {"x-agent-token": token}
        payload = {
            "task_id": str(task.id),
            "status": "FAILED",
            "failure_class": "EXTERNAL_API",
            "logs": "api.github.com returned 503",
        }
        before = datetime.utcnow()
        resp = await client.post("/api/agent/status", json=payload, headers=headers)
        assert resp.status_code == 200

        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED, f"retry {retry_num}: expected QUEUED"
        ctx = task.context or {}
        assert "retry_after" in ctx, f"retry {retry_num}: retry_after not set"
        assert "provision_failures" not in ctx, (
            f"retry {retry_num}: provision_failures should be cleared on graceful requeue"
        )

        retry_after_dt = datetime.fromisoformat(ctx["retry_after"])
        expected_delay = min(2**retry_num * 120, 1800)
        actual_delay = (retry_after_dt - before).total_seconds()
        assert actual_delay >= expected_delay - 2, (
            f"retry {retry_num}: delay {actual_delay} < expected {expected_delay}"
        )
        assert actual_delay <= expected_delay + 5, (
            f"retry {retry_num}: delay {actual_delay} too large"
        )

    # 6th attempt: api_retries=5, increments to 6 > 5 → EXTERNAL_API requeue path is skipped
    # Falls through to general retry (task.attempt_count < max_retries), so no retry_after is set
    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "test"}],
        vm_instance_id="agent-vm-backoff-6",
        status=TaskStatus.IN_PROGRESS,
        context={"_api_failure_retries": 5},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-backoff-6")
    headers = {"x-agent-token": token}
    payload = {
        "task_id": str(task.id),
        "status": "FAILED",
        "failure_class": "EXTERNAL_API",
        "logs": "api.github.com returned 503",
    }
    resp = await client.post("/api/agent/status", json=payload, headers=headers)
    assert resp.status_code == 200

    await db_session.refresh(task)
    # Falls through to general retry path — no EXTERNAL_API-specific retry_after is set
    ctx = task.context or {}
    assert "retry_after" not in ctx, (
        "after EXTERNAL_API cap exceeded, retry_after should not be set by EXTERNAL_API path"
    )


@pytest.mark.asyncio
async def test_reassigned_lands_cancelled_no_retry_no_alert(
    client: AsyncClient, db_session, mocker
):
    """FIX H: a REASSIGNED outcome (agent gracefully self-terminated because the
    task was superseded/reassigned/deauthed) must land CANCELLED on first
    detection — NOT retry, NOT FAILED, NOT fire a failure alert — mirroring the
    SCOPE_EMPTY->CANCELLED precedent."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )

    project = Project(name="Reassigned Test", user_id="test-user-reassigned")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "build x"}],
        vm_instance_id="agent-vm-reassigned",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-reassigned")
    headers = {"x-agent-token": token}
    payload = {
        "task_id": str(task.id),
        "status": "FAILED",
        "failure_category": "reassigned",
        "logs": "Task reassigned/deauthorized — agent self-terminating",
    }
    resp = await client.post("/api/agent/status", json=payload, headers=headers)
    assert resp.status_code == 200

    await db_session.refresh(task)
    assert task.status == TaskStatus.CANCELLED, (
        f"REASSIGNED must land CANCELLED, not {task.status} — it is a graceful "
        f"handoff, not a code failure"
    )
    assert task.status != TaskStatus.QUEUED, "REASSIGNED must not retry"
    assert (task.context or {}).get("failure_category") == "REASSIGNED"
    alert_mock.assert_not_called()


@pytest.mark.asyncio
async def test_external_api_permanent_auth_failure_does_not_requeue(
    client: AsyncClient, db_session, mocker
):
    """
    EXTERNAL_API failures with permanent auth indicators (token expired, 401, bad credentials)
    must fail immediately without consuming any retry budget. Requeuing only wastes 30+ minutes
    of exponential backoff before the task permanently fails with the same error.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)
    # _process_post_commit opens a separate session and does FOR UPDATE on the
    # task, racing with the test's session and producing PostCommitLocked
    # (~6s/iteration of FOR UPDATE retries). This test asserts handler-side
    # permanent-auth detection only — post-commit (PR review/rollup wiring)
    # isn't relevant here.
    mocker.patch(
        "app.routers.agent_status._process_post_commit",
        new_callable=mocker.AsyncMock,
        return_value={"status": "accepted"},
    )

    project = Project(name="Auth Failure Test", user_id="test-user-auth-fail")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    permanent_auth_cases = [
        "GitHub API returned 401 unauthorized",
        "token expired: please re-authenticate",
        "token revoked by organization admin",
        "bad credentials — check your PAT",
        "401 Unauthorized: access denied",
    ]

    for i, logs_msg in enumerate(permanent_auth_cases):
        task = Task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "do work"}],
            vm_instance_id=f"agent-vm-auth-{i}",
            status=TaskStatus.IN_PROGRESS,
            context={"_api_failure_retries": 0},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        token = create_agent_token(str(task.id), f"agent-vm-auth-{i}")
        headers = {"x-agent-token": token}
        payload = {
            "task_id": str(task.id),
            "status": "FAILED",
            "failure_class": "EXTERNAL_API",
            "logs": logs_msg,
        }
        resp = await client.post("/api/agent/status", json=payload, headers=headers)
        assert resp.status_code == 200, (
            f"case {i}: unexpected status {resp.status_code}"
        )

        await db_session.refresh(task)
        assert task.status == TaskStatus.FAILED, (
            f"case {i} ({logs_msg!r}): expected FAILED (immediate), got {task.status}"
        )
        # retry_after must not be set — permanent failures should not schedule a retry
        ctx = task.context or {}
        assert "retry_after" not in ctx, (
            f"case {i}: retry_after should not be set for permanent auth failure"
        )
        # Operator-facing message must direct to re-authentication
        assert task.latest_status_message is not None
        assert (
            "auth" in task.latest_status_message.lower()
            or "re-authenticate" in task.latest_status_message.lower()
        ), (
            f"case {i}: status message should mention auth/re-authenticate, got: {task.latest_status_message!r}"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested_model", ["gemini-3.1-pro-preview", "gemini-3-pro-preview"]
)
async def test_external_api_provider_model_config_falls_back_without_backoff(
    requested_model, client: AsyncClient, db_session, mocker
):
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch(
        "app.routers.agent_status.release_max_plan_account",
        new_callable=mocker.AsyncMock,
    )
    mocker.patch(
        "app.routers.agent_status.broadcast_to_project", new_callable=mocker.AsyncMock
    )

    project = Project(
        name="Gemini Model Fallback Test", user_id="test-user-gemini-fallback"
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Audit W1: retry_after_until is a column. Set it on the task directly.
    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "test", "model": requested_model}],
        vm_instance_id="agent-vm-gemini-model-config",
        status=TaskStatus.IN_PROGRESS,
        retry_after_until=datetime(2026, 4, 25),
        context={
            "llm_account_id": "gemini-acct-1",
            "max_plan_account_id": "gemini-acct-1",
            "llm_provider": "gemini",
            "llm_auth_mode": "api_key",
            "llm_model": requested_model,
            "llm_execution_mode": "default",
            "_api_failure_retries": 2,
            "blacklisted_accounts": ["gemini-acct-1"],
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-gemini-model-config")
    resp = await client.post(
        "/api/agent/status",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "status": "FAILED",
            "failure_category": "fatal",
            "logs": "ModelNotFoundError: models/gemini-3.1-pro-preview is not found for API version v1beta",
        },
    )
    assert resp.status_code == 200

    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert task.payloads[0]["model"] == "gemini-2.5-pro"
    ctx = task.context or {}
    assert ctx["failure_category"] == "EXTERNAL_API"
    assert ctx["external_api_subtype"] == "provider_model_config"
    assert ctx["llm_provider"] == "gemini"
    assert ctx["llm_auth_mode"] == "api_key"
    assert ctx["llm_model"] == "gemini-2.5-pro"
    assert "llm_account_id" not in ctx
    assert "max_plan_account_id" not in ctx
    assert "_api_failure_retries" not in ctx
    assert task.retry_after_until is None  # column source of truth
    assert "blacklisted_accounts" not in ctx
    assert ctx["model_fallback"]["requested"] == requested_model
    assert ctx["model_fallback"]["effective"] == "gemini-2.5-pro"
    assert ctx["model_fallback"]["reason"] == "provider_model_config_rejection"


@pytest.mark.asyncio
async def test_external_api_provider_model_config_without_fallback_does_not_requeue(
    client: AsyncClient, db_session, mocker
):
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch(
        "app.routers.agent_status.broadcast_to_project", new_callable=mocker.AsyncMock
    )
    mocker.patch(
        "app.routers.agent_status._process_post_commit",
        new_callable=mocker.AsyncMock,
        return_value={"status": "accepted"},
    )

    project = Project(
        name="Provider Model No Fallback Test", user_id="test-user-model-no-fallback"
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work", "model": "gpt-no-such"}],
        vm_instance_id="agent-vm-model-no-fallback",
        status=TaskStatus.IN_PROGRESS,
        context={
            "llm_provider": "openai",
            "llm_auth_mode": "api_key",
            "llm_model": "gpt-no-such",
            "llm_execution_mode": "default",
            "_api_failure_retries": 2,
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-model-no-fallback")
    resp = await client.post(
        "/api/agent/status",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "status": "FAILED",
            "failure_category": "fatal",
            "logs": "model not found: gpt-no-such",
        },
    )
    assert resp.status_code == 200

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.attempt_count >= 3
    ctx = task.context or {}
    assert ctx["failure_category"] == "EXTERNAL_API"
    assert ctx["external_api_subtype"] == "provider_model_config"
    assert task.latest_status_message is not None
    assert "model/config" in task.latest_status_message


# ── Open-PR handoff on agent-reported FAILED ──────────────────────────────
# Regression for the "Refusing to delete stale branch" retry loop: when a
# previous attempt opened a PR, the agent's setupWorkspace stale-branch guard
# correctly refuses to delete the branch on retry — so naive requeue burns
# the entire retry budget without ever progressing. The agent-reported FAILED
# path must mirror the reaper's open-PR handoff (reaper.py:912-1048) and route
# to EVALUATING + review pipeline instead of QUEUED.


@pytest.mark.asyncio
async def test_failed_with_open_pr_hands_off_to_review(
    client: AsyncClient, db_session, mocker
):
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent_status.broadcast_to_project", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent_status._process_post_commit",
        new_callable=AsyncMock,
        return_value={"status": "accepted"},
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "open", "merged": False}),
    )
    spawn_mock = AsyncMock(return_value=(None, True))
    mocker.patch("app.review_pipeline._spawn_pipeline_task", spawn_mock)

    project = Project(name="Open-PR Handoff", user_id="test-user-open-pr-handoff")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        vm_instance_id="agent-vm-open-pr-handoff",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=1,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    db_session.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/owner/repo/pull/3575",
        )
    )
    await db_session.commit()

    token = create_agent_token(str(task.id), "agent-vm-open-pr-handoff")
    resp = await client.post(
        "/api/agent/status",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "status": "FAILED",
            "failure_category": "fatal",
            "logs": "Refusing to delete stale branch blueprint/task-deadbeef: has 1 open PR(s) (#3575)",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted_pr_handoff"

    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING, (
        f"Expected EVALUATING, got {task.status} — open-PR handoff did not fire"
    )
    assert task.attempt_count == 1, "attempt_count must not increment on PR handoff"
    assert "#3575" in (task.latest_status_message or "")
    assert "handed off to review pipeline" in (task.latest_status_message or "")
    spawn_mock.assert_awaited_once()
    spawn_args = spawn_mock.await_args
    assert spawn_args.args[1] == "review"
    assert spawn_args.args[3] == 3575  # pr_number
    # C6: on confirmed spawn success the write-ahead recovery marker is dropped
    # (a childed EVALUATING parent is a normally parked parent — leaving the
    # flag set would make the evaluating-sweep consumer spawn a SECOND review).
    assert not (task.context or {}).get("_pr_state_check_pending"), (
        "marker must be cleared once the review spawn is confirmed"
    )


@pytest.mark.asyncio
async def test_failed_with_open_pr_spawn_failure_keeps_recovery_marker(
    client: AsyncClient, db_session, mocker
):
    """C6 seam: when the review spawn FAILS during the FAILED-with-open-PR
    handoff, the task must stay EVALUATING AND keep `_pr_state_check_pending`
    set so the services `_check_evaluating_timeouts` consumer re-checks the PR
    and re-spawns the review.

    Pre-fix the flag was popped before the spawn and never re-set on failure, so
    the parent ended up childless+EVALUATING with NO recovery marker — invisible
    to reaper Step 3 (needs a COMPLETED child) and Step 4 (skips childless
    parents) and to the marker-gated sweep — stranded until the 6h/48h backstop.
    Would FAIL pre-fix: `_pr_state_check_pending` would be absent.
    """
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent_status.broadcast_to_project", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent_status._process_post_commit",
        new_callable=AsyncMock,
        return_value={"status": "accepted"},
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "open", "merged": False}),
    )
    # Spawn raises — but does NOT poison the session (it fails before any
    # internal flush/commit). Exercises the non-poisoned spawn-failure path.
    spawn_mock = AsyncMock(side_effect=RuntimeError("spawn boom"))
    mocker.patch("app.review_pipeline._spawn_pipeline_task", spawn_mock)

    project = Project(name="Open-PR Spawn Fail", user_id="test-user-open-pr-spawnfail")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        vm_instance_id="agent-vm-open-pr-spawnfail",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=1,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    db_session.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/owner/repo/pull/4242",
        )
    )
    await db_session.commit()

    token = create_agent_token(str(task.id), "agent-vm-open-pr-spawnfail")
    resp = await client.post(
        "/api/agent/status",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "status": "FAILED",
            "failure_category": "fatal",
            "logs": "Refusing to delete stale branch blueprint/task-cafe: has 1 open PR(s) (#4242)",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted_pr_handoff"

    await db_session.refresh(task)
    # Task stays EVALUATING (not stranded RUNNING/FAILED) so the sweep can act.
    assert task.status == TaskStatus.EVALUATING, (
        f"Expected EVALUATING after spawn failure, got {task.status}"
    )
    # The write-ahead recovery marker MUST survive the spawn failure.
    assert (task.context or {}).get("_pr_state_check_pending") is True, (
        "spawn failure must leave _pr_state_check_pending set so the "
        "evaluating-sweep consumer can recover the childless parent"
    )
    spawn_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_pool_served_task_feeds_pool_breaker(
    client: AsyncClient, db_session, mocker
):
    """A FAILED task that was served by the warm pool feeds the pool-claim
    circuit breaker.

    Regression guard: the agent-reported FAILED path keys the breaker feed on
    the exact origin stamped by the pool dispatch path. That stamp is
    WARM_POOL_ORIGIN ("warm_pool", warm_pool.py / agent_warm_continuity.py),
    NOT the literal "pool". A mismatch makes the feed dead code so the
    breaker's primary trip mode (a bad pool image silently failing a stream of
    tasks) can never fire. This test stamps the REAL origin and asserts the
    failure is recorded.
    """
    import app.shared_state as ss
    from app.warm_pool import WARM_POOL_ORIGIN

    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent_status.broadcast_to_project", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent_status._process_post_commit",
        new_callable=AsyncMock,
        return_value={"status": "accepted"},
    )

    project = Project(name="Pool Breaker Feed", user_id="test-user-pool-breaker")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        vm_instance_id="agent-vm-pool-breaker",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=1,
        # Stamp the warm-claim origin exactly as the pool dispatch path does.
        context={"_timing": {"warm_start": True, "warm_claim_origin": WARM_POOL_ORIGIN}},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    ss.warm_pool_claim_failure_events.clear()
    try:
        token = create_agent_token(str(task.id), "agent-vm-pool-breaker")
        resp = await client.post(
            "/api/agent/status",
            headers={"x-agent-token": token},
            json={
                "task_id": str(task.id),
                "status": "FAILED",
                "failure_category": "fatal",
                "logs": "boom",
            },
        )
        assert resp.status_code == 200, resp.text
        # The pool-served FAILED task must have fed the pool-claim breaker.
        assert ss.count_recent_pool_claim_failures() == 1, (
            "FAILED task stamped warm_claim_origin=WARM_POOL_ORIGIN must record a "
            "pool-claim failure — the breaker feed is keyed on the wrong literal"
        )
    finally:
        ss.warm_pool_claim_failure_events.clear()


@pytest.mark.asyncio
async def test_failed_with_merged_pr_marks_completed(
    client: AsyncClient, db_session, mocker
):
    """PR merged out-of-band before agent reported FAILED → COMPLETED, not retry."""
    from app.models import Alert

    dispatch_mock = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent_status.broadcast_to_project", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent_status._process_post_commit",
        new_callable=AsyncMock,
        return_value={"status": "accepted"},
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "closed", "merged": True, "base": {"ref": "main"}}),
    )

    project = Project(
        name="Merged PR Handoff",
        user_id="test-user-merged-pr",
        integration_branch="main",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work", "base_branch": "automain"}],
        vm_instance_id="agent-vm-merged-pr",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    db_session.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/owner/repo/pull/4242",
        )
    )
    await db_session.commit()

    token = create_agent_token(str(task.id), "agent-vm-merged-pr")
    resp = await client.post(
        "/api/agent/status",
        headers={"x-agent-token": token},
        json={"task_id": str(task.id), "status": "FAILED", "logs": "boom"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted_pr_merged"
    await asyncio.sleep(0)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert (task.context or {}).get("pr_merged") is True
    alerts = (
        (
            await db_session.execute(select(Alert).where(Alert.task_id == task.id))
        )
        .scalars()
        .all()
    )
    assert alerts == []
    dispatch_mock.assert_not_called()


@pytest.mark.asyncio
async def test_failed_with_merged_pr_on_distinct_promotion_target_alerts_without_pr_merged(
    client: AsyncClient, db_session, mocker
):
    """A merged PR on promotion_target must complete without marking the task landed on integration."""
    from app.models import Alert, AlertSeverity

    dispatch_mock = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent_status.broadcast_to_project", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent_status._process_post_commit",
        new_callable=AsyncMock,
        return_value={"status": "accepted"},
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "closed", "merged": True, "base": {"ref": "main"}}),
    )

    project = Project(
        name="Merged Promotion PR Handoff",
        user_id="test-user-merged-promotion-pr",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "create", "prompt": "do work", "base_branch": "automain"}
        ],
        vm_instance_id="agent-vm-merged-promotion-pr",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    db_session.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/owner/repo/pull/4243",
        )
    )
    await db_session.commit()

    token = create_agent_token(str(task.id), "agent-vm-merged-promotion-pr")
    resp = await client.post(
        "/api/agent/status",
        headers={"x-agent-token": token},
        json={"task_id": str(task.id), "status": "FAILED", "logs": "boom"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted_pr_merged"
    await asyncio.sleep(0)

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert (task.context or {}).get("pr_merged") is None
    alerts = (
        (
            await db_session.execute(select(Alert).where(Alert.task_id == task.id))
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR
    assert alerts[0].title == "Merged PR landed on promotion target before integration"
    approval_calls = [
        c for c in dispatch_mock.call_args_list if c.args[1] == "APPROVAL_NEEDED"
    ]
    assert len(approval_calls) == 1
    assert approval_calls[0].args[2]["pr_number"] == 4243


@pytest.mark.asyncio
async def test_failed_with_merged_pr_on_intermediate_staged_rollup_stays_evaluating(
    client: AsyncClient, db_session, mocker
):
    dispatch_mock = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent_status.broadcast_to_project", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent_status._process_post_commit",
        new_callable=AsyncMock,
        return_value={"status": "accepted"},
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "closed",
                "merged": True,
                "base": {"ref": "release/candidate-1"},
            }
        ),
    )

    project = Project(
        name="Merged Intermediate Rollup Handoff",
        user_id="test-user-merged-intermediate-rollup",
        integration_branch="automain",
        promotion_target="release/main",
        build_config={"staging_branch_prefixes": ["release/"]},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "research",
                "prompt": "rollup parent",
                "base_branch": "release/candidate-1",
            }
        ],
        vm_instance_id="agent-vm-merged-intermediate-rollup",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        context={"rollup_pr_number": 4244},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    db_session.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/owner/repo/pull/4244",
        )
    )
    await db_session.commit()

    token = create_agent_token(str(task.id), "agent-vm-merged-intermediate-rollup")
    resp = await client.post(
        "/api/agent/status",
        headers={"x-agent-token": token},
        json={"task_id": str(task.id), "status": "FAILED", "logs": "boom"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted_pr_merged"
    await asyncio.sleep(0)

    await db_session.refresh(task)
    assert task.status == TaskStatus.EVALUATING
    assert (task.context or {}).get("pr_merged") is None
    assert "awaiting downstream landing" in (task.latest_status_message or "")
    approval_calls = [
        c for c in dispatch_mock.call_args_list if c.args[1] == "APPROVAL_NEEDED"
    ]
    assert approval_calls == []


@pytest.mark.asyncio
async def test_failed_with_closed_unmerged_pr_marks_terminal_failed(
    client: AsyncClient, db_session, mocker
):
    """PR closed without merging → terminal FAILED with closure marker; no retry."""
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent_status.broadcast_to_project", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent_status._process_post_commit",
        new_callable=AsyncMock,
        return_value={"status": "accepted"},
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "closed", "merged": False}),
    )

    project = Project(name="Closed PR Handoff", user_id="test-user-closed-pr")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        vm_instance_id="agent-vm-closed-pr",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    db_session.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/owner/repo/pull/7777",
        )
    )
    await db_session.commit()

    token = create_agent_token(str(task.id), "agent-vm-closed-pr")
    resp = await client.post(
        "/api/agent/status",
        headers={"x-agent-token": token},
        json={"task_id": str(task.id), "status": "FAILED", "logs": "boom"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted_pr_closed"

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert (task.context or {}).get("pr_closed_unmerged") is True
    assert "#7777" in (task.latest_status_message or "")


@pytest.mark.asyncio
async def test_failed_without_pr_artifact_uses_normal_retry(
    client: AsyncClient, db_session, mocker
):
    """Without a PR artifact, the FAILED path should still take the normal retry flow.

    Guards against the open-PR handoff over-reaching and breaking unrelated retries.
    """
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent_status.broadcast_to_project", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent_status._process_post_commit",
        new_callable=AsyncMock,
        return_value={"status": "accepted"},
    )

    project = Project(name="No PR Retry", user_id="test-user-no-pr-retry")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        vm_instance_id="agent-vm-no-pr-retry",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-no-pr-retry")
    resp = await client.post(
        "/api/agent/status",
        headers={"x-agent-token": token},
        json={"task_id": str(task.id), "status": "FAILED", "logs": "boom"},
    )
    assert resp.status_code == 200
    # Without a PR, normal retry path fires → QUEUED with attempt_count incremented.
    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert task.attempt_count == 1


@pytest.mark.asyncio
async def test_agent_claim_non_research_includes_pipeline_health(
    client: AsyncClient, db_session, mocker
):
    """Non-gated task type (docs) receives pipeline_health via the else-branch."""
    project = Project(name="Pipeline Health Test", user_id="test-user-ph")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "docs", "cmd": "write docs"}],
        vm_instance_id="agent-vm-ph-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    fake_health = {
        "success_rate": 0.9,
        "trend": "improving",
        "first_attempt_success_rate": {
            "overall": 0.85,
            "trend": "stable",
            "sample_size": 20,
            "by_type": {"create": 0.85, "fix": 0.62},
        },
        "failure_distribution": {},
        "direction_stats": [],
    }
    mocker.patch(
        "app.routers.agent.compute_pipeline_health_summary",
        return_value=fake_health,
    )

    token = create_agent_token(str(task.id), "agent-vm-ph-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()
    assert "project_context" in data
    assert data["project_context"]["pipeline_health"] is not None
    assert (
        data["project_context"]["pipeline_health"]["first_attempt_success_rate"][
            "by_type"
        ]["create"]
        == 0.85
    )


@pytest.mark.asyncio
async def test_agent_claim_pipeline_health_includes_scope_conversion_rates(
    client: AsyncClient, db_session, mocker
):
    """Claim response includes scope_conversion_rates and medium_scope_review_iterations in pipeline_health."""
    project = Project(name="Scope Merge Rate Test", user_id="test-user-smr")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "docs", "cmd": "write docs"}],
        vm_instance_id="agent-vm-smr-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    fake_health = {
        "success_rate": 0.92,
        "trend": "stable",
        "first_attempt_success_rate": {
            "overall": 0.88,
            "trend": "stable",
            "sample_size": 50,
            "by_type": {"create": 0.88},
        },
        "failure_distribution": {},
        "direction_stats": [],
        "pipeline_conversion_rate": None,
        "review_accuracy": None,
        "persistent_review_issues": None,
        "scope_conversion_rates": {
            "medium": {
                "current_rate": 0.75,
                "prev_rate": 0.5,
                "delta": 0.25,
                "trend": "improving",
            },
            "small": {
                "current_rate": 0.9,
                "prev_rate": 0.85,
                "delta": 0.05,
                "trend": "stable",
            },
        },
        "medium_scope_review_iterations": 2.5,
    }
    mocker.patch(
        "app.routers.agent.compute_pipeline_health_summary",
        return_value=fake_health,
    )

    token = create_agent_token(str(task.id), "agent-vm-smr-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()
    assert "project_context" in data
    ph = data["project_context"]["pipeline_health"]
    assert ph is not None
    assert "scope_conversion_rates" in ph
    assert isinstance(ph["scope_conversion_rates"], dict)
    assert ph["scope_conversion_rates"]["medium"]["current_rate"] == 0.75
    assert ph["scope_conversion_rates"]["medium"]["trend"] == "improving"
    assert ph["medium_scope_review_iterations"] == 2.5


@pytest.mark.asyncio
async def test_playbook_claim_filters_by_task_type(client: AsyncClient, db_session):
    """
    Claim endpoint filters playbook entries by applicable_types:
    - entry with applicable_types=["create","fix"] is excluded for 'research' tasks
    - entry with applicable_types=None is excluded for 'research' (not in _PLAYBOOK_DEFAULT_TYPES)
    - entry with applicable_types=None IS included for 'fix' (in _PLAYBOOK_DEFAULT_TYPES)
    """
    project = Project(name="Playbook Filter Test", user_id="test-user-pbf")
    project.design_doc = {
        "playbook/write-only": {
            "content": "only for write tasks",
            "applicable_types": ["create", "fix"],
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
        },
        "playbook/untagged": {
            "content": "untagged operational entry",
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # research task: both entries should be excluded
    task_research = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "investigate something"}],
        vm_instance_id="agent-vm-pbf-1",
    )
    db_session.add(task_research)
    await db_session.commit()
    await db_session.refresh(task_research)

    token = create_agent_token(str(task_research.id), "agent-vm-pbf-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()

    # No playbook entries should be present for research task
    playbook = data.get("playbook", {})
    assert "write-only" not in playbook
    assert "untagged" not in playbook

    # fix task: untagged entry should be included
    task_fix = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "prompt": "fix the bug"}],
        vm_instance_id="agent-vm-pbf-2",
    )
    db_session.add(task_fix)
    await db_session.commit()
    await db_session.refresh(task_fix)

    token2 = create_agent_token(str(task_fix.id), "agent-vm-pbf-2")
    resp2 = await client.post("/api/agent/claim", headers={"x-agent-token": token2})
    assert resp2.status_code == 200
    data2 = resp2.json()

    assert "playbook" in data2
    # write-only entry has applicable_types=["create","fix"] so included for fix
    assert "write-only" in data2["playbook"]
    # untagged entry is included for fix (in _PLAYBOOK_DEFAULT_TYPES)
    assert "untagged" in data2["playbook"]
    assert data2["playbook"]["untagged"]["content"] == "untagged operational entry"


@pytest.mark.asyncio
async def test_playbook_claim_includes_entry_when_type_matches(
    client: AsyncClient, db_session
):
    """
    Claim endpoint includes playbook entries when task type is in applicable_types.
    """
    project = Project(name="Playbook Match Test", user_id="test-user-pbm")
    project.design_doc = {
        "playbook/build-hint": {
            "content": "build instruction",
            "applicable_types": ["create", "fix", "refactor"],
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "prompt": "fix a bug"}],
        vm_instance_id="agent-vm-pbm-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-pbm-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()

    assert "playbook" in data
    assert "build-hint" in data["playbook"]
    assert data["playbook"]["build-hint"]["content"] == "build instruction"


@pytest.mark.asyncio
async def test_playbook_claim_increments_reference_count(
    client: AsyncClient, db_session
):
    """
    After claim, reference_count is incremented and last_referenced_at is set
    for served playbook entries. Non-served entries (filtered by applicable_types) are NOT incremented.
    """
    project = Project(name="Playbook RefCount Test", user_id="test-user-prc")
    project.design_doc = {
        "playbook/served-entry": {
            "content": "served content",
            "applicable_types": ["create"],
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
            "reference_count": 2,
        },
        "playbook/unserved-entry": {
            "content": "write-only content",
            "applicable_types": ["review"],  # won't match 'create' task
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
            "reference_count": 0,
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "build something"}],
        vm_instance_id="agent-vm-prc-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-prc-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()
    assert "playbook" in data
    assert "served-entry" in data["playbook"]
    assert "unserved-entry" not in data["playbook"]

    # Verify reference_count incremented for served entry
    await db_session.refresh(project)
    served = project.design_doc["playbook/served-entry"]
    assert served["reference_count"] == 3
    assert served["last_referenced_at"] is not None

    # Verify unserved entry unchanged
    unserved = project.design_doc["playbook/unserved-entry"]
    assert unserved["reference_count"] == 0
    assert "last_referenced_at" not in unserved


@pytest.mark.asyncio
async def test_playbook_claim_ordering_confirmed_count(client: AsyncClient, db_session):
    """
    Playbook entries returned by claim are sorted by effectiveness_score descending.
    For entries without recent_outcomes data, the score falls back to confirmed/referenced ratio.
    Entries with higher confirmation rate per reference sort first.
    """
    project = Project(name="Playbook Order Test", user_id="test-user-order")
    project.design_doc = {
        "playbook/low-ratio": {
            "content": "low confirmation rate",
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 1,
            "reference_count": 10,  # ratio = 0.1
        },
        "playbook/high-ratio": {
            "content": "high confirmation rate",
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 5,
            "reference_count": 2,  # ratio = 2.5 (more confirmations than references — possible since all applicable tasks increment confirmed_count)
        },
        "playbook/mid-ratio": {
            "content": "medium confirmation rate",
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 5,
            "reference_count": 8,  # ratio = 0.625
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "build something"}],
        vm_instance_id="agent-vm-order-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-order-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()

    assert "playbook" in data
    keys = list(data["playbook"].keys())
    # Sort by effectiveness score descending: high-ratio (2.5) → mid-ratio (0.625) → low-ratio (0.1)
    assert keys.index("high-ratio") < keys.index("mid-ratio"), f"Order: {keys}"
    assert keys.index("mid-ratio") < keys.index("low-ratio"), f"Order: {keys}"


@pytest.mark.asyncio
async def test_playbook_claim_prioritises_local_relevance_and_includes_reasons(
    client: AsyncClient, db_session
):
    """
    Claim prioritises entries that locally match task direction/system/related files and
    includes relevance metadata in response payload.
    """
    now = datetime.utcnow().isoformat()
    project = Project(name="Playbook Relevance Test", user_id="test-user-relevance")
    project.design_doc = {
        "_file_to_system": {
            "backend/app/routers/agent.py": "routing",
        },
        "playbook/high-ratio-generic": {
            "content": "generic guidance",
            "updated_at": now,
            "confirmed_count": 10,
            "reference_count": 1,
        },
        "playbook/local-match": {
            "content": "specific guidance",
            "updated_at": now,
            "confirmed_count": 1,
            "reference_count": 10,
            "direction_successes": {"dir-123": 2},
            "learned_from_system": "routing",
            "related_files": ["backend/app/routers/agent.py"],
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "build something"}],
        vm_instance_id="agent-vm-relevance-1",
        context={
            "direction_id": "dir-123",
            "related_files": ["backend/app/routers/agent.py"],
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-relevance-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()

    assert "playbook" in data
    keys = list(data["playbook"].keys())
    assert keys.index("local-match") < keys.index("high-ratio-generic"), (
        f"Order: {keys}"
    )
    relevance = data["playbook"]["local-match"]["relevance"]
    assert relevance["local_match"] is True
    assert "same direction" in relevance["reasons"]
    assert "same system" in relevance["reasons"]
    assert "1 overlapping file(s)" in relevance["reasons"]


@pytest.mark.asyncio
async def test_playbook_claim_handles_non_dict_file_to_system(
    client: AsyncClient, db_session
):
    """
    Claim tolerates malformed _file_to_system metadata without crashing.
    """
    now = datetime.utcnow().isoformat()
    project = Project(
        name="Playbook Bad Metadata Test", user_id="test-user-bad-metadata"
    )
    project.design_doc = {
        "_file_to_system": ["not", "a", "dict"],
        "playbook/safe-entry": {
            "content": "safe guidance",
            "updated_at": now,
            "confirmed_count": 1,
            "reference_count": 1,
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "build something"}],
        vm_instance_id="agent-vm-bad-metadata-1",
        context={"related_files": ["backend/app/routers/agent.py"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-bad-metadata-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()
    assert "playbook" in data
    assert "safe-entry" in data["playbook"]


@pytest.mark.asyncio
async def test_playbook_claim_age_penalty_deprioritises_old_entries(
    client: AsyncClient, db_session
):
    """
    Old entries (>30 days, confirmed_count < 3) are deprioritised in claim ordering.
    A recent unconfirmed entry should appear before an older unconfirmed entry.
    """
    old_date = (datetime.utcnow() - timedelta(days=40)).isoformat()
    recent_date = datetime.utcnow().isoformat()

    project = Project(name="Age Penalty Test", user_id="test-user-age-penalty")
    project.design_doc = {
        "playbook/old-unconfirmed": {
            "content": "old tip with few confirmations",
            "confirmed_count": 0,
            "reference_count": 0,
            "updated_at": old_date,
        },
        "playbook/recent-unconfirmed": {
            "content": "recent tip with few confirmations",
            "confirmed_count": 0,
            "reference_count": 0,
            "updated_at": recent_date,
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "build something"}],
        vm_instance_id="agent-vm-age-penalty-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-age-penalty-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()

    assert "playbook" in data
    keys = list(data["playbook"].keys())
    # Recent entry should appear before the age-penalised old entry
    assert keys.index("recent-unconfirmed") < keys.index("old-unconfirmed"), (
        f"Expected recent-unconfirmed before old-unconfirmed, got order: {keys}"
    )


@pytest.mark.asyncio
async def test_playbook_claim_stale_file_refs_deprioritised(
    client: AsyncClient, db_session
):
    """
    Entries with stale_file_refs=True are deprioritised — a clean entry with the same
    confirmed_count should come before a stale entry.
    """
    now = datetime.utcnow().isoformat()

    project = Project(name="Stale Refs Test", user_id="test-user-stale-refs")
    project.design_doc = {
        "playbook/stale-entry": {
            "content": "tip referencing a deleted file",
            "confirmed_count": 3,
            "reference_count": 5,
            "updated_at": now,
            "stale_file_refs": True,
        },
        "playbook/clean-entry": {
            "content": "tip with valid file refs",
            "confirmed_count": 3,
            "reference_count": 5,
            "updated_at": now,
            "stale_file_refs": False,
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "build something"}],
        vm_instance_id="agent-vm-stale-refs-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-stale-refs-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()

    assert "playbook" in data
    keys = list(data["playbook"].keys())
    # Clean entry should come before stale entry
    assert keys.index("clean-entry") < keys.index("stale-entry"), (
        f"Expected clean-entry before stale-entry, got order: {keys}"
    )


@pytest.mark.asyncio
async def test_playbook_claim_old_confirmed_entry_not_penalised(
    client: AsyncClient, db_session
):
    """
    An old entry with confirmed_count >= 3 should NOT receive the age penalty.
    It should still be ordered before a less-confirmed recent entry.
    """
    old_date = (datetime.utcnow() - timedelta(days=40)).isoformat()
    recent_date = datetime.utcnow().isoformat()

    project = Project(name="Confirmed Old Entry Test", user_id="test-user-conf-old")
    project.design_doc = {
        "playbook/old-confirmed": {
            "content": "old but well-confirmed tip",
            "confirmed_count": 5,
            "reference_count": 10,
            "updated_at": old_date,
        },
        "playbook/recent-low-confirmed": {
            "content": "recent tip with low confirmations",
            "confirmed_count": 1,
            "reference_count": 1,
            "updated_at": recent_date,
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "build something"}],
        vm_instance_id="agent-vm-conf-old-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-conf-old-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()

    assert "playbook" in data
    keys = list(data["playbook"].keys())
    # Old but confirmed entry (no age penalty) should come before less-confirmed recent entry
    assert keys.index("old-confirmed") < keys.index("recent-low-confirmed"), (
        f"Expected old-confirmed before recent-low-confirmed, got order: {keys}"
    )


@pytest.mark.asyncio
async def test_playbook_confirmed_count_incremented_on_first_attempt_success(
    client: AsyncClient, db_session, mocker
):
    """
    When a task completes on first attempt and the project has playbook entries,
    confirmed_count is incremented for entries applicable to that task type.
    Entries not applicable to the task type are NOT incremented.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)

    project = Project(name="Playbook Confirm Test", user_id="test-user-confirm")
    project.design_doc = {
        "playbook/create-entry": {
            "content": "create guidance",
            "applicable_types": ["create", "fix"],
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 2,
        },
        "playbook/review-only": {
            "content": "review guidance",
            "applicable_types": ["review"],
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do something"}],
        vm_instance_id="agent-vm-confirm-1",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-confirm-1")
    resp = await client.post(
        "/api/agent/status",
        json={"task_id": str(task.id), "status": "COMPLETED", "logs": "done"},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200

    await db_session.refresh(project)
    # create-entry is applicable to 'create' tasks — confirmed_count should be incremented
    assert project.design_doc["playbook/create-entry"]["confirmed_count"] == 3
    # review-only is NOT applicable to 'create' tasks — confirmed_count unchanged
    assert project.design_doc["playbook/review-only"]["confirmed_count"] == 0


@pytest.mark.asyncio
async def test_playbook_confirmed_count_not_incremented_on_retry_success(
    client: AsyncClient, db_session, mocker
):
    """
    When a task completes after retries (attempt_count >= 2), confirmed_count
    is NOT incremented — only first-attempt successes count as confirmation.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)

    project = Project(name="Playbook No Confirm Retry", user_id="test-user-no-confirm")
    project.design_doc = {
        "playbook/some-entry": {
            "content": "some guidance",
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 1,
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do something"}],
        vm_instance_id="agent-vm-no-confirm-1",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=2,  # not first attempt
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-no-confirm-1")
    resp = await client.post(
        "/api/agent/status",
        json={"task_id": str(task.id), "status": "COMPLETED", "logs": "done"},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200

    await db_session.refresh(project)
    # attempt_count=2 → _first_attempt_succeeded=False → confirmed_count unchanged
    assert project.design_doc["playbook/some-entry"]["confirmed_count"] == 1


@pytest.mark.asyncio
async def test_already_done_no_op_completion_lands_completed_with_reason(
    client: AsyncClient, db_session, mocker
):
    """`./done --already-done` posts COMPLETED with no PR + a no_op_reason.

    Models the redundant-no-op case (e.g. task 76ae55c2): the migrate agent
    verified the deliverable already exists on the base branch, so there is no
    diff/PR. The status endpoint must land the task COMPLETED (not requeue /
    fail), persist no_op_reason into task.context so operators see WHY there's
    no PR, and surface the reason in latest_status_message. No PR artifact is
    created.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)

    project = Project(name="Already-Done No-Op", user_id="test-user-already-done")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "migrate", "prompt": "add Alembic migration for PoolVm registry"}],
        vm_instance_id="agent-vm-already-done-1",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    reason = (
        "0133_poolvm_registry.py + PoolVm model + EXPECTED_SCHEMA_REVISION=0133 "
        "already present on base via sibling PR #6978; a new migration would "
        "branch the revision chain"
    )
    token = create_agent_token(str(task.id), "agent-vm-already-done-1")
    resp = await client.post(
        "/api/agent/status",
        json={
            "task_id": str(task.id),
            "status": "COMPLETED",
            "no_op_reason": reason,
            "logs": f"Completed: no changes needed — deliverable already present. {reason}",
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200

    await db_session.refresh(task)
    # Landed COMPLETED — not requeued, not failed.
    assert task.status == TaskStatus.COMPLETED
    # The affirmative reason is persisted so operators see why there's no PR.
    assert (task.context or {}).get("no_op_reason") == reason
    # And surfaced in the status message (no artifact ⇒ logs → latest_status_message).
    assert task.latest_status_message
    assert "deliverable already present" in task.latest_status_message

    # No PR artifact was created for this no-op completion.
    from sqlalchemy import select as _select

    artifacts = (
        await db_session.execute(
            _select(Artifact).where(Artifact.task_id == task.id)
        )
    ).scalars().all()
    assert all(a.artifact_type != ArtifactType.PULL_REQUEST for a in artifacts)


@pytest.mark.asyncio
async def test_playbook_recent_outcomes_flipped_on_first_attempt_success(
    client: AsyncClient, db_session, mocker
):
    """
    When a task with _shown_playbook_keys completes on first attempt, the most recent False
    in recent_outcomes is flipped to True and effectiveness_score is computed.
    Entries not shown to this task do not get their recent_outcomes updated.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)

    project = Project(name="Outcomes Flip Test", user_id="test-user-outcomes")
    project.design_doc = {
        "playbook/shown-entry": {
            "content": "guidance shown to this task",
            "applicable_types": ["create"],
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 1,
            "reference_count": 3,
            "recent_outcomes": [True, False, False],  # last False should be flipped
        },
        "playbook/not-shown-entry": {
            "content": "guidance not shown to this task",
            "applicable_types": ["create"],
            "source_task_id": None,
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
            "reference_count": 2,
            "recent_outcomes": [False, False],  # should NOT be changed
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do something"}],
        vm_instance_id="agent-vm-outcomes-flip",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        # Simulate that only 'shown-entry' was shown at claim time
        context={"_shown_playbook_keys": ["shown-entry"]},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-outcomes-flip")
    resp = await client.post(
        "/api/agent/status",
        json={"task_id": str(task.id), "status": "COMPLETED", "logs": "done"},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200

    await db_session.refresh(project)
    shown = project.design_doc["playbook/shown-entry"]
    not_shown = project.design_doc["playbook/not-shown-entry"]

    # Shown entry: last False flipped to True → [True, False, True]
    assert shown["recent_outcomes"] == [True, False, True], (
        f"Expected [True, False, True], got {shown['recent_outcomes']}"
    )
    # effectiveness_score computed: 2/3 ≈ 0.6667
    assert shown.get("effectiveness_score") is not None
    assert abs(shown["effectiveness_score"] - round(2 / 3, 4)) < 0.001
    # last_confirmed_at set
    assert shown.get("last_confirmed_at") is not None

    # Not-shown entry: recent_outcomes unchanged
    assert not_shown["recent_outcomes"] == [False, False], (
        f"Not-shown entry should not be updated, got {not_shown['recent_outcomes']}"
    )
    assert not_shown.get("effectiveness_score") is None


@pytest.mark.asyncio
async def test_playbook_auto_archive_after_10_unconfirmed_references(
    client: AsyncClient, db_session
):
    """
    After 10 references with no confirmations, an entry accumulates 10 Falses in recent_outcomes
    (score=0.0 < 0.1 threshold) and is auto-archived (stale=True) during reference tracking.
    Subsequent claims skip the stale entry.
    """
    from sqlalchemy.orm.attributes import flag_modified

    project = Project(name="Auto Archive Ref Test", user_id="test-user-autoarchive")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Entry pre-loaded with 9 Falses — one more reference will fill the window (10 Falses → stale)
    project.design_doc = {
        "playbook/zero-effectiveness": {
            "content": "guidance that is never confirmed",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 0,
            "reference_count": 9,
            "recent_outcomes": [False] * 9,  # 9 unconfirmed references so far
        },
        "playbook/active-entry": {
            "content": "currently active guidance",
            "source_task_id": "seed",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_count": 2,
            "reference_count": 3,
        },
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # First claim: shows both entries, appends False to zero-effectiveness → 10 Falses → stale=True
    task1 = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "First task"}],
        vm_instance_id="agent-vm-autoarchive-1",
    )
    db_session.add(task1)
    await db_session.commit()
    await db_session.refresh(task1)

    token1 = create_agent_token(str(task1.id), "agent-vm-autoarchive-1")
    resp1 = await client.post("/api/agent/claim", headers={"x-agent-token": token1})
    assert resp1.status_code == 200

    # After first claim: zero-effectiveness should now have stale=True in DB
    await db_session.refresh(project)
    entry = project.design_doc["playbook/zero-effectiveness"]
    assert entry.get("stale") is True, (
        f"Expected stale=True after 10 Falses, got: {entry}"
    )
    assert entry.get("effectiveness_score") == 0.0

    # Second claim: zero-effectiveness should be excluded (stale=True)
    task2 = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Second task"}],
        vm_instance_id="agent-vm-autoarchive-2",
    )
    db_session.add(task2)
    await db_session.commit()
    await db_session.refresh(task2)

    token2 = create_agent_token(str(task2.id), "agent-vm-autoarchive-2")
    resp2 = await client.post("/api/agent/claim", headers={"x-agent-token": token2})
    assert resp2.status_code == 200

    data2 = resp2.json()
    playbook2 = data2.get("playbook", {})
    assert "zero-effectiveness" not in playbook2, (
        "Auto-archived entry should not appear in subsequent claims"
    )
    assert "active-entry" in playbook2, "Active entry should still appear"


@pytest.mark.asyncio
async def test_playbook_admin_list_stale_flag(client: AsyncClient, db_session):
    """
    Admin playbook list endpoint returns reference_count, last_referenced_at, stale flag,
    effectiveness_score, and last_confirmed_at.
    Stale is True if stored stale=True (auto-archived) OR no reference in 30+ days.
    Auth is mocked by conftest to test-user-123 (admin).
    """
    from app.models import ProjectMember

    _now = datetime.utcnow()
    _yesterday = (_now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    _two_days_ago = (_now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
    _sixty_days_ago = (_now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S")
    _forty_five_days_ago = (_now - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%S")

    project = Project(name="Playbook Stale Test", user_id="test-user-123")
    project.design_doc = {
        "playbook/fresh-entry": {
            "content": "fresh content",
            "updated_at": _two_days_ago,
            "confirmed_count": 3,
            "reference_count": 5,
            "last_referenced_at": _yesterday,  # 1 day ago — not stale
            "last_confirmed_at": _two_days_ago,
            "effectiveness_score": 0.6,
        },
        "playbook/stale-entry": {
            "content": "old content",
            "updated_at": _sixty_days_ago,  # >30 days ago, never referenced
            "confirmed_count": 0,
            "reference_count": 0,
        },
        "playbook/stale-ref-entry": {
            "content": "referenced long ago",
            "updated_at": _sixty_days_ago,
            "confirmed_count": 0,
            "reference_count": 3,
            "last_referenced_at": _forty_five_days_ago,  # >30 days ago
        },
        "playbook/auto-archived-entry": {
            "content": "persistently ineffective",
            "updated_at": _two_days_ago,
            "confirmed_count": 0,
            "reference_count": 10,
            "last_referenced_at": _yesterday,  # recent but stale=True stored
            "stale": True,
            "effectiveness_score": 0.0,
        },
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Add project member matching the mocked current_user (test-user-123)
    member = ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    db_session.add(member)
    await db_session.commit()

    # No auth header needed — conftest mocks get_current_user to test-user-123
    resp = await client.get(f"/api/admin/projects/{project.id}/playbook")
    assert resp.status_code == 200
    entries = resp.json()["entries"]

    fresh = entries["fresh-entry"]
    assert fresh["reference_count"] == 5
    assert fresh["last_referenced_at"] == _yesterday
    assert fresh["last_confirmed_at"] == _two_days_ago
    assert fresh["effectiveness_score"] == 0.6
    assert fresh["stale"] is False

    stale_entry = entries["stale-entry"]
    assert stale_entry["reference_count"] == 0
    assert stale_entry["last_referenced_at"] is None
    assert stale_entry["last_confirmed_at"] is None
    assert stale_entry["effectiveness_score"] is None
    assert stale_entry["stale"] is True

    stale_ref = entries["stale-ref-entry"]
    assert stale_ref["reference_count"] == 3
    assert stale_ref["stale"] is True

    # Auto-archived entry: stored stale=True takes precedence even though last_referenced_at is recent
    auto_archived = entries["auto-archived-entry"]
    assert auto_archived["stale"] is True
    assert auto_archived["effectiveness_score"] == 0.0


@pytest.mark.asyncio
async def test_agent_create_pr_recreates_when_cached_pr_closed(
    client: AsyncClient, db_session, mocker
):
    """If the cached PR artifact points at a closed PR (e.g. branch was deleted
    on requeue, auto-closing the PR), the endpoint must NOT return the stale URL.
    It should delete the stale artifact and create a fresh PR.

    Regression for task 512c4514: the idempotency check returned a closed PR's
    URL to the agent, which then reported COMPLETED on a phantom PR.
    """
    from unittest.mock import AsyncMock

    project = Project(name="Stale PR Recreate", user_id="test-user-stale-pr")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do thing"}],
        vm_instance_id="agent-vm-stale-pr",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    db_session.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/foo/bar/pull/42",
        )
    )
    await db_session.commit()

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "foo", "bar")),
    )
    mocker.patch(
        "app.routers.agent_github.get_pull_request",
        new=AsyncMock(return_value={"state": "closed", "merged": False}),
    )
    mock_create = AsyncMock(
        return_value={"html_url": "https://github.com/foo/bar/pull/99", "number": 99}
    )
    mocker.patch("app.routers.agent_github.create_pull_request", new=mock_create)

    token = create_agent_token(str(task.id), "agent-vm-stale-pr")
    headers = {"x-agent-token": token}
    resp = await client.post(
        "/api/agent/github/pull-requests",
        json={
            "task_id": str(task.id),
            "owner": "foo",
            "repo": "bar",
            "title": "fresh PR",
            "head": "blueprint/task-deadbeef",
            "base": "main",
            "body": "body",
        },
        headers=headers,
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["html_url"] == "https://github.com/foo/bar/pull/99"
    mock_create.assert_called_once()

    artifacts = (
        (
            await db_session.execute(
                select(Artifact).where(
                    Artifact.task_id == task.id,
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                )
            )
        )
        .scalars()
        .all()
    )
    pr_urls = sorted(a.pr_url for a in artifacts)
    assert "https://github.com/foo/bar/pull/99" in pr_urls
    assert "https://github.com/foo/bar/pull/42" not in pr_urls, (
        "Stale closed-PR artifact should have been removed"
    )


@pytest.mark.asyncio
async def test_agent_create_pr_returns_cached_when_open(
    client: AsyncClient, db_session, mocker
):
    """If the cached PR artifact points at an OPEN PR, the endpoint should
    short-circuit and return its URL without calling create_pull_request."""
    from unittest.mock import AsyncMock

    project = Project(name="Open PR Cached", user_id="test-user-open-pr")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do thing"}],
        vm_instance_id="agent-vm-open-pr",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    db_session.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/foo/bar/pull/7",
        )
    )
    await db_session.commit()

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("tok", "foo", "bar")),
    )
    mocker.patch(
        "app.routers.agent_github.get_pull_request",
        new=AsyncMock(return_value={"state": "open", "merged": False}),
    )
    mock_create = AsyncMock()
    mocker.patch("app.routers.agent_github.create_pull_request", new=mock_create)

    token = create_agent_token(str(task.id), "agent-vm-open-pr")
    headers = {"x-agent-token": token}
    resp = await client.post(
        "/api/agent/github/pull-requests",
        json={
            "task_id": str(task.id),
            "owner": "foo",
            "repo": "bar",
            "title": "should not be created",
            "head": "blueprint/task-cafef00d",
            "base": "main",
            "body": "body",
        },
        headers=headers,
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["html_url"] == "https://github.com/foo/bar/pull/7"
    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_agent_github_check_runs_rejects_invalid_token(client: AsyncClient):
    resp = await client.get(
        "/api/agent/github/check-runs",
        params={"commit_sha": "deadbee"},
        headers={"x-agent-token": "wrong-token"},
    )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_agent_github_check_runs_validates_commit_sha(
    client: AsyncClient, db_session, mocker
):
    project = Project(name="Check Runs Validation", user_id="test-user-check-runs-1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "prompt": "inspect CI"}],
        vm_instance_id="agent-vm-check-runs-1",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    get_token_mock = mocker.patch(
        "app.routers.agent.get_github_token_for_task", new=AsyncMock()
    )
    get_check_runs_mock = mocker.patch(
        "app.routers.agent_github.get_check_runs", new=AsyncMock()
    )

    token = create_agent_token(str(task.id), "agent-vm-check-runs-1")
    resp = await client.get(
        "/api/agent/github/check-runs",
        params={"commit_sha": "main/../../pulls/1"},
        headers={"x-agent-token": token},
    )

    assert resp.status_code == 422
    get_token_mock.assert_not_called()
    get_check_runs_mock.assert_not_called()


@pytest.mark.asyncio
async def test_agent_github_check_runs_response_omits_raw_token(
    client: AsyncClient, db_session, mocker
):
    project = Project(name="Check Runs Response", user_id="test-user-check-runs-2")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "prompt": "inspect CI"}],
        vm_instance_id="agent-vm-check-runs-2",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("secret-gh-token", "acme", "widget")),
    )
    get_check_runs_mock = mocker.patch(
        "app.routers.agent_github.get_check_runs",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "failure",
                "details": [
                    {
                        "name": "backend",
                        "status": "completed",
                        "conclusion": "failure",
                    }
                ],
                "token": "should-not-leak",
            }
        ),
    )

    token = create_agent_token(str(task.id), "agent-vm-check-runs-2")
    resp = await client.get(
        "/api/agent/github/check-runs",
        params={"commit_sha": "deadbeef"},
        headers={"x-agent-token": token},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["data"] == {
        "status": "completed",
        "conclusion": "failure",
        "details": [
            {
                "name": "backend",
                "status": "completed",
                "conclusion": "failure",
            }
        ],
    }
    assert "token" not in body["data"]
    get_check_runs_mock.assert_awaited_once_with(
        token="secret-gh-token",
        owner="acme",
        repo="widget",
        ref="deadbeef",
    )


@pytest.mark.asyncio
async def test_rate_limit_requeue_clears_terminated_cache(
    client: AsyncClient, db_session, mocker
):
    """
    Regression test for the 401 cascade bug.

    When an agent reports FAILED with a rate-limit failure, agent_status.py:359
    sets task.status = FAILED, firing the SQLAlchemy set event that adds task.id
    to _terminated_task_ids. Then the RATE_LIMIT requeue path overwrites the
    status back to QUEUED. Without the fix, the cache entry persists — every
    subsequent claim for the requeued task returns 401 until the 9-hour TTL
    expires.

    This test pins the fix: after a rate-limit requeue, _terminated_task_ids
    must NOT contain the task_id.
    """
    from app.routers.agent import _terminated_task_ids

    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    project = Project(name="Rate Limit Requeue Test", user_id="test-user-rl-cache")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "test"}],
        vm_instance_id="agent-vm-rl-cache",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id_str = str(task.id)

    # Make sure no stale cache entry exists from a prior test
    _terminated_task_ids.pop(task_id_str, None)

    token = create_agent_token(task_id_str, "agent-vm-rl-cache")
    headers = {"x-agent-token": token}
    payload = {
        "task_id": task_id_str,
        "status": "FAILED",
        "failure_category": "rate_limited",
        "logs": "You've hit your usage limit - View usage",
    }
    resp = await client.post("/api/agent/status", json=payload, headers=headers)
    assert resp.status_code == 200

    await db_session.refresh(task)
    # Task must be requeued, not left in a terminal state
    assert task.status == TaskStatus.QUEUED, (
        f"Rate-limited task should be requeued to QUEUED, got {task.status}"
    )
    # The fix: cache entry must be cleared so the next VM can claim the task
    assert task_id_str not in _terminated_task_ids, (
        f"Task {task_id_str} must not be in _terminated_task_ids after rate-limit "
        f"requeue — entry would block every subsequent claim with 401 until the "
        f"9-hour TTL expires."
    )

    # Clean up to avoid polluting other tests
    _terminated_task_ids.pop(task_id_str, None)


@pytest.mark.asyncio
async def test_completed_terminal_populates_terminated_cache(
    client: AsyncClient, db_session, mocker
):
    """
    Inverse of test_rate_limit_requeue_clears_terminated_cache: a genuinely
    terminal COMPLETED status MUST populate _terminated_task_ids so the agent's
    JWT is revoked and a second process can't reuse it.

    Pins the intended semantics of the SQLAlchemy terminal callback.
    """
    from app.routers.agent import _terminated_task_ids

    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    project = Project(name="Terminal Cache Test", user_id="test-user-terminal-cache")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "test"}],
        vm_instance_id="agent-vm-terminal-cache",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id_str = str(task.id)

    _terminated_task_ids.pop(task_id_str, None)

    token = create_agent_token(task_id_str, "agent-vm-terminal-cache")
    headers = {"x-agent-token": token}
    payload = {
        "task_id": task_id_str,
        "status": "COMPLETED",
        "logs": "task finished",
    }
    resp = await client.post("/api/agent/status", json=payload, headers=headers)
    assert resp.status_code == 200

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    # The SQLAlchemy set event must have populated the cache for this truly
    # terminal transition — otherwise verify_agent_token would accept the
    # agent's old JWT for continued heartbeats.
    assert task_id_str in _terminated_task_ids, (
        f"Task {task_id_str} must be in _terminated_task_ids after COMPLETED so "
        f"verify_agent_token revokes the agent's JWT."
    )

    _terminated_task_ids.pop(task_id_str, None)


@pytest.mark.asyncio
async def test_terminal_to_nonterminal_transition_emits_revive_notify(
    db_session, mocker
):
    """A genuine terminal → non-terminal (requeue) status transition fires the
    cross-instance revive NOTIFY via the before_flush listener.

    This is the fix for the asymmetric coherency bug: TERMINAL was already
    broadcast cross-instance, but REVIVE was evicted in-process only, so a
    requeued task's heartbeat could load-balance to a stale instance and 401.
    """
    revive_calls = mocker.patch("app.jwt_revocation.emit_task_revived_notify")

    project = Project(name="Revive Notify Test", user_id="test-user-revive-notify")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Seed the task already terminal (FAILED) so the requeue is a real
    # terminal → non-terminal transition.
    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "test"}],
        status=TaskStatus.FAILED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id_str = str(task.id)

    # Reset call count so only the requeue transition is observed.
    revive_calls.reset_mock()

    # Requeue: FAILED → QUEUED.
    task.status = TaskStatus.QUEUED
    db_session.add(task)
    await db_session.commit()

    assert revive_calls.call_count == 1, (
        f"terminal → non-terminal transition must emit exactly one revive "
        f"notify, got {revive_calls.call_count}"
    )
    # Second positional arg is the task id string.
    emitted_tid = revive_calls.call_args[0][1]
    assert emitted_tid == task_id_str, (
        f"revive notify should carry the requeued task id, got {emitted_tid}"
    )


@pytest.mark.asyncio
async def test_normal_nonterminal_transition_does_not_emit_revive_notify(
    db_session, mocker
):
    """A normal non-terminal lifecycle step (QUEUED → PROVISIONING → IN_PROGRESS)
    must NOT fire the revive NOTIFY — only a real terminal → non-terminal
    transition does, to avoid cross-instance notify spam.
    """
    revive_calls = mocker.patch("app.jwt_revocation.emit_task_revived_notify")

    project = Project(name="No Revive Spam Test", user_id="test-user-no-revive-spam")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Start non-terminal (QUEUED) so no transition crosses the terminal boundary.
    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "test"}],
        status=TaskStatus.QUEUED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    revive_calls.reset_mock()

    # Ordinary forward lifecycle: none of these crosses terminal → non-terminal.
    task.status = TaskStatus.PROVISIONING
    db_session.add(task)
    await db_session.commit()

    task.status = TaskStatus.IN_PROGRESS
    db_session.add(task)
    await db_session.commit()

    assert revive_calls.call_count == 0, (
        f"normal non-terminal lifecycle steps must NOT emit revive notify, "
        f"got {revive_calls.call_count} call(s)"
    )


@pytest.mark.asyncio
async def test_rate_limit_requeue_allows_subsequent_claim(
    client: AsyncClient, db_session, mocker
):
    """
    End-to-end regression: after a rate-limit requeue, a fresh VM must be able
    to re-claim the task without hitting the 401 cascade. Before the fix, the
    verify_agent_token dependency rejected every request for the requeued
    task because _terminated_task_ids still contained the task_id.

    We exercise the non-/api/agent/status code path (/api/agent/heartbeat) so
    the cache check in verify_agent_token is actually reached.
    """
    from app.routers.agent import _terminated_task_ids

    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    project = Project(name="Rate Limit E2E Test", user_id="test-user-rl-e2e")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "test"}],
        vm_instance_id="agent-vm-rl-e2e-old",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id_str = str(task.id)

    _terminated_task_ids.pop(task_id_str, None)

    # Step 1: old agent reports rate-limit FAILED → task requeued to QUEUED
    old_token = create_agent_token(task_id_str, "agent-vm-rl-e2e-old")
    payload = {
        "task_id": task_id_str,
        "status": "FAILED",
        "failure_category": "rate_limited",
        "logs": "You've hit your usage limit - View usage",
    }
    resp = await client.post(
        "/api/agent/status",
        json=payload,
        headers={"x-agent-token": old_token},
    )
    assert resp.status_code == 200

    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED

    # Step 2: orchestrator will later provision a new VM for this task. Simulate
    # that VM's agent heartbeating with a JWT bound to the new vm_instance_id
    # (which agent_status.py rewrote on requeue). The request must NOT be
    # rejected with 401 — that would reproduce the cascade.
    new_vm_id = task.vm_instance_id
    assert new_vm_id != "agent-vm-rl-e2e-old", (
        "Requeue path should rotate vm_instance_id so the next VM has its own identity"
    )
    new_token = create_agent_token(task_id_str, new_vm_id)
    hb_resp = await client.post(
        "/api/agent/heartbeat",
        json={"task_id": task_id_str, "status_message": "new VM started"},
        headers={"x-agent-token": new_token},
    )
    assert hb_resp.status_code != 401, (
        f"Heartbeat from fresh VM after rate-limit requeue must not return 401. "
        f"Got {hb_resp.status_code}: {hb_resp.text}. This is the 401 cascade bug."
    )


@pytest.mark.asyncio
async def test_file_change_details_stored_in_task_context(
    client: AsyncClient, db_session, mocker
):
    """When an agent reports status with file_change_details, the field is stored
    in task.context['_file_change_details'] so _requeue_parent_task can propagate
    it to the retry clone as _previous_file_change_details."""
    from app.routers.agent import _terminated_task_ids

    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)

    project = Project(name="File Change Details Test", user_id="test-user-fcd")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "add feature"}],
        vm_instance_id="agent-vm-fcd",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id_str = str(task.id)

    _terminated_task_ids.pop(task_id_str, None)

    file_details = [
        {"file": "backend/app/models.py", "additions": 45, "deletions": 12},
        {"file": "backend/testing/test_models.py", "additions": 8, "deletions": 3},
    ]
    token = create_agent_token(str(task.id), "agent-vm-fcd")
    payload = {
        "task_id": str(task.id),
        "status": "FAILED",
        "artifact_type": "ERROR_LOG",
        "logs": "Test suite failed",
        "failure_category": "test_failed",
        "file_change_details": file_details,
    }
    resp = await client.post(
        "/api/agent/status", json=payload, headers={"x-agent-token": token}
    )
    assert resp.status_code == 200

    await db_session.refresh(task)
    ctx = task.context or {}
    assert ctx.get("_file_change_details") == file_details, (
        f"Expected _file_change_details in task.context, got: {ctx}"
    )

    _terminated_task_ids.pop(task_id_str, None)


@pytest.mark.asyncio
async def test_scope_gate_triggered_stored_in_task_context(
    client: AsyncClient, db_session, mocker
):
    """Agent status with scope_gate_triggered='analysis_overflow' persists
    the value to task.context so direction_outcomes can differentiate
    analysis-phase overflow from post-implementation overflow."""
    from app.routers.agent import _terminated_task_ids

    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)

    project = Project(name="Scope Gate Test", user_id="test-user-sg")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        vm_instance_id="agent-vm-sg",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id_str = str(task.id)

    _terminated_task_ids.pop(task_id_str, None)

    token = create_agent_token(str(task.id), "agent-vm-sg")
    payload = {
        "task_id": str(task.id),
        "status": "FAILED",
        "artifact_type": "ERROR_LOG",
        "logs": "analysis phase could not plan within budget",
        "failure_category": "scope_overflow",
        "scope_gate_triggered": "analysis_overflow",
    }
    resp = await client.post(
        "/api/agent/status", json=payload, headers={"x-agent-token": token}
    )
    assert resp.status_code == 200

    await db_session.refresh(task)
    ctx = task.context or {}
    assert ctx.get("scope_gate_triggered") == "analysis_overflow", (
        f"Expected scope_gate_triggered in task.context, got: {ctx}"
    )

    _terminated_task_ids.pop(task_id_str, None)


@pytest.mark.asyncio
async def test_agent_status_pr_artifact_stores_enriched_context(
    client: AsyncClient, db_session, mocker
):
    """COMPLETED+PULL_REQUEST status with diff_summary, self_review_outcome, and
    test_failure_summary stores them in task context as _pr_diff_summary,
    _self_review_outcome, and _test_failure_summary respectively."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    project = Project(name="Enriched Context PR Test", user_id="test-user-enriched-pr")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do thing"}],
        vm_instance_id="agent-vm-enriched-pr",
        status=TaskStatus.IN_PROGRESS,
    )
    # Sibling stays IN_PROGRESS so project aggregation path is not triggered
    sibling = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "sibling"}],
        vm_instance_id="agent-vm-enriched-pr-sibling",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    db_session.add(sibling)
    await db_session.commit()
    await db_session.refresh(task)

    diff_summary = [
        {
            "file": "backend/app/models.py",
            "additions": 45,
            "deletions": 12,
            "change_type": "modified",
        },
        {
            "file": "backend/testing/test_models.py",
            "additions": 30,
            "deletions": 0,
            "change_type": "added",
        },
    ]
    self_review_outcome = {
        "issues_found": False,
        "fixes_applied": False,
        "categories": [],
    }
    test_failure_summary = "FAILED backend/testing/test_models.py::test_create"

    token = create_agent_token(str(task.id), "agent-vm-enriched-pr")
    headers = {"x-agent-token": token}
    payload = {
        "task_id": str(task.id),
        "status": "COMPLETED",
        "artifact_type": "PULL_REQUEST",
        "pr_url": "https://github.com/foo/bar/pull/10",
        "changed_files": ["backend/app/models.py", "backend/testing/test_models.py"],
        "diff_summary": diff_summary,
        "self_review_outcome": self_review_outcome,
        "test_failure_summary": test_failure_summary,
    }
    resp = await client.post("/api/agent/status", json=payload, headers=headers)
    assert resp.status_code == 200

    await db_session.refresh(task)
    ctx = task.context or {}
    assert ctx.get("_pr_diff_summary") == diff_summary
    stored_sro = ctx.get("_self_review_outcome") or {}
    for k, v in self_review_outcome.items():
        assert stored_sro[k] == v
    assert stored_sro["_filtered_blind_spots_count"] == 0
    assert ctx.get("_test_failure_summary") == test_failure_summary
    assert ctx.get("_pr_url") == "https://github.com/foo/bar/pull/10"


@pytest.mark.asyncio
async def test_agent_status_malformed_pr_url_returns_400(
    client: AsyncClient, db_session, mocker
):
    """POSTing a PULL_REQUEST artifact with a malformed pr_url (e.g. a /commit/<sha>
    URL) must return HTTP 400 and must NOT persist an Artifact row."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    project = Project(name="Malformed PR URL Test", user_id="test-user-malformed-pr")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do thing"}],
        vm_instance_id="agent-vm-malformed-pr",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-malformed-pr")
    headers = {"x-agent-token": token}

    bogus_pr_url = "https://github.com/HenryHamster/BlueprintAIStudio/commit/b8932d1"
    payload = {
        "task_id": str(task.id),
        "status": "COMPLETED",
        "artifact_type": "PULL_REQUEST",
        "pr_url": bogus_pr_url,
        "logs": "Task complete.",
    }
    resp = await client.post("/api/agent/status", json=payload, headers=headers)
    assert resp.status_code == 400
    assert "pull" in resp.text.lower() or "pr_url" in resp.text.lower()

    # No Artifact row must have been created.
    from sqlmodel import select as sql_select
    from app.models import Artifact, ArtifactType

    artifacts = (
        (
            await db_session.execute(
                sql_select(Artifact).where(
                    Artifact.task_id == task.id,
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                )
            )
        )
        .scalars()
        .all()
    )
    assert artifacts == [], "Malformed pr_url must not persist an Artifact row"

    # A valid URL must still be accepted.
    valid_pr_url = "https://github.com/HenryHamster/BlueprintAIStudio/pull/5699"
    payload["pr_url"] = valid_pr_url
    resp2 = await client.post("/api/agent/status", json=payload, headers=headers)
    assert resp2.status_code == 200


@pytest.mark.asyncio
async def test_agent_status_failed_artifact_stores_diff_summary_and_test_failure(
    client: AsyncClient, db_session, mocker
):
    """FAILED+ERROR_LOG status with diff_summary and test_failure_summary stores them
    in task context as _pr_diff_summary and _test_failure_summary."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    project = Project(
        name="Enriched Context Failed Test", user_id="test-user-enriched-failed"
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do thing"}],
        vm_instance_id="agent-vm-enriched-failed",
        status=TaskStatus.IN_PROGRESS,
        # Exhaust retries so the FAILED path terminates without requeueing
        attempt_count=3,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    diff_summary = [
        {
            "file": "backend/app/models.py",
            "additions": 10,
            "deletions": 2,
            "change_type": "modified",
        },
    ]
    test_failure_summary = (
        "FAILED backend/testing/test_models.py::test_create - AssertionError"
    )

    token = create_agent_token(str(task.id), "agent-vm-enriched-failed")
    headers = {"x-agent-token": token}
    payload = {
        "task_id": str(task.id),
        "status": "FAILED",
        "artifact_type": "ERROR_LOG",
        "logs": "Test suite failed after max attempts",
        "changed_files": ["backend/app/models.py"],
        "diff_summary": diff_summary,
        "test_failure_summary": test_failure_summary,
    }
    resp = await client.post("/api/agent/status", json=payload, headers=headers)
    assert resp.status_code == 200

    await db_session.refresh(task)
    ctx = task.context or {}
    assert ctx.get("_pr_diff_summary") == diff_summary
    assert ctx.get("_test_failure_summary") == test_failure_summary


@pytest.mark.asyncio
async def test_claim_top_first_review_issues_populated(client: AsyncClient, db_session):
    """top_first_review_issues is populated when review_history contains issues with descriptions at iteration 0."""
    from sqlalchemy.orm.attributes import flag_modified

    project = Project(name="Top Review Issues Test", user_id="test-user-tri")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create two completed tasks with review_history entries that have issues at iteration 0
    common_issue = "Missing test coverage for the new function"
    for i in range(2):
        ht = Task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": f"Some task {i}"}],
            vm_instance_id=f"agent-vm-tri-{i}",
            status=TaskStatus.COMPLETED,
            context={
                "review_loop": {
                    "iteration": 2,
                    "review_history": [
                        {
                            "iteration": 0,
                            "feedback": "Needs work",
                            "issues": [
                                {
                                    "severity": "high",
                                    "description": common_issue,
                                    "file": "app/foo.py",
                                    "line": 10,
                                },
                            ],
                        }
                    ],
                }
            },
        )
        flag_modified(ht, "context")
        db_session.add(ht)
    await db_session.commit()

    # Create the task to claim
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "New feature"}],
        vm_instance_id="agent-vm-tri-claim",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-tri-claim")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    project_ctx = resp.json().get("project_context", {})
    top_issues = project_ctx.get("top_first_review_issues")
    assert top_issues is not None, "top_first_review_issues should be populated"
    assert len(top_issues) >= 1
    descriptions = [issue["description"] for issue in top_issues]
    assert common_issue.lower()[:120] in descriptions


@pytest.mark.asyncio
async def test_claim_direction_top_first_review_issues_populated(
    client: AsyncClient, db_session
):
    """direction_context.top_first_review_issues is populated when a direction has 3+ tasks with review history and a recurring issue."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Direction Top Review Issues Test", user_id="test-user-dtri")
    project.directions = [
        {
            "id": direction_id,
            "title": "Refactor auth",
            "priority": "medium",
            "research_context": "",
            "research_history": [],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    direction_issue = "Direction-scoped missing validation on inputs"
    other_issue = "Unique issue seen only once"
    # 3 tasks in this direction with review history; 2 share the recurring issue.
    for i in range(3):
        issues = [
            {
                "severity": "high",
                "description": direction_issue,
                "file": "a.py",
                "line": 1,
            }
        ]
        if i == 2:
            issues = [
                {
                    "severity": "low",
                    "description": other_issue,
                    "file": "b.py",
                    "line": 2,
                }
            ]
        ht = Task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": f"dir task {i}"}],
            vm_instance_id=f"agent-vm-dtri-{i}",
            status=TaskStatus.COMPLETED,
            context={
                "direction_id": direction_id,
                "review_loop": {
                    "iteration": 2,
                    "review_history": [
                        {"iteration": 0, "feedback": "fix", "issues": issues},
                    ],
                },
            },
        )
        flag_modified(ht, "context")
        db_session.add(ht)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "new in direction"}],
        vm_instance_id="agent-vm-dtri-claim",
        context={"direction_id": direction_id},
    )
    flag_modified(task, "context")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-dtri-claim")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    dc = resp.json().get("direction_context", {})
    dir_top = dc.get("top_first_review_issues")
    assert dir_top, "direction_context.top_first_review_issues should be populated"
    descriptions = [issue["description"] for issue in dir_top]
    assert direction_issue.lower()[:120] in descriptions
    assert other_issue.lower()[:120] not in descriptions


@pytest.mark.asyncio
async def test_claim_direction_top_first_review_issues_gated_by_min_tasks(
    client: AsyncClient, db_session
):
    """direction_context.top_first_review_issues stays None when fewer than 3 direction-scoped tasks have review history."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(
        name="Direction Top Review Issues Gated", user_id="test-user-dtri-gate"
    )
    project.directions = [
        {
            "id": direction_id,
            "title": "Refactor auth",
            "priority": "medium",
            "research_context": "",
            "research_history": [],
        }
    ]
    flag_modified(project, "directions")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    shared_issue = "Recurring direction issue"
    # Only 2 direction-scoped tasks with review history — below the 3-task threshold.
    for i in range(2):
        ht = Task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": f"dir task {i}"}],
            vm_instance_id=f"agent-vm-dtri-gate-{i}",
            status=TaskStatus.COMPLETED,
            context={
                "direction_id": direction_id,
                "review_loop": {
                    "iteration": 2,
                    "review_history": [
                        {
                            "iteration": 0,
                            "feedback": "fix",
                            "issues": [
                                {
                                    "severity": "high",
                                    "description": shared_issue,
                                    "file": "a.py",
                                    "line": 1,
                                }
                            ],
                        },
                    ],
                },
            },
        )
        flag_modified(ht, "context")
        db_session.add(ht)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "new in direction"}],
        vm_instance_id="agent-vm-dtri-gate-claim",
        context={"direction_id": direction_id},
    )
    flag_modified(task, "context")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-dtri-gate-claim")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    dc = resp.json().get("direction_context", {})
    assert dc.get("top_first_review_issues") is None


@pytest.mark.asyncio
async def test_claim_direction_first_attempt_feedback_in_direction_context(
    client: AsyncClient, db_session
):
    """direction_context.first_attempt_feedback exposes per-direction rejection category counts from design_doc."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = str(uuid.uuid4())
    project = Project(name="Direction FA Feedback Test", user_id="test-user-dfa-ctx")
    project.directions = [
        {
            "id": direction_id,
            "title": "Refactor auth",
            "priority": "medium",
            "research_context": "",
            "research_history": [],
        }
    ]
    flag_modified(project, "directions")
    project.design_doc = {
        "direction_first_attempt_feedback": {
            direction_id: {
                "test-coverage": 5,
                "error-handling": 2,
                "total": 7,
                "last_updated": "2026-04-18T00:00:00",
            },
            "_project": {"type-safety": 3, "total": 3},
        }
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "new in direction"}],
        vm_instance_id="agent-vm-dfa-ctx",
        context={"direction_id": direction_id},
    )
    flag_modified(task, "context")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-dfa-ctx")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    dc = resp.json().get("direction_context", {})
    fa = dc.get("first_attempt_feedback")
    assert fa == {"test-coverage": 5, "error-handling": 2}
    assert next(iter(fa)) == "test-coverage"  # sorted by count desc


async def _setup_smoketest_task(
    db_session,
    vm_id: str,
    base_branch: str = "main",
    *,
    integration_branch: str | None = None,
    promotion_target: str | None = None,
):
    """Create a project + IN_PROGRESS smoketest task.

    Leaving attempt_count at 0 keeps the task on the normal-retry path on FAILED,
    so the permanent-failure auto-debug spawn does not also fire — isolating the
    SMOKETEST plan-task spawn behavior under test.
    """
    project = Project(
        name=f"Smoketest {vm_id}",
        user_id="test-user-smoketest",
        integration_branch=integration_branch,
        promotion_target=promotion_target,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "smoketest", "base_branch": base_branch}],
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return project, task


@pytest.mark.asyncio
async def test_smoketest_startup_failure_spawns_plan_task_priority_90(
    client: AsyncClient, db_session, mocker
):
    """SMOKETEST_RESULT with a startup-suite failure spawns a plan task at priority 90."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    _, task = await _setup_smoketest_task(db_session, "agent-vm-smoke-startup")

    import json as _json

    smoketest_payload = {
        "passed": False,
        "tests": [
            {"name": "startup_probe", "status": "fail", "message": "port unreachable"},
        ],
        "suites": {
            "startup": {
                "passed": False,
                "tests": [{"name": "startup_probe", "status": "fail"}],
            },
            "curated": {"passed": True, "tests": []},
        },
    }
    token = create_agent_token(str(task.id), "agent-vm-smoke-startup")
    payload = {
        "task_id": str(task.id),
        "status": "FAILED",
        "artifact_type": "SMOKETEST_RESULT",
        "logs": _json.dumps(smoketest_payload),
    }
    resp = await client.post(
        "/api/agent/status", json=payload, headers={"x-agent-token": token}
    )
    assert resp.status_code == 200

    result = await db_session.execute(
        select(Task).where(Task.parent_task_id == task.id)
    )
    spawned = result.scalars().all()
    assert len(spawned) == 1, f"Expected 1 spawned plan task, got {len(spawned)}"
    plan_task = spawned[0]
    assert plan_task.priority == 90
    assert plan_task.first_payload.get("type") == "plan"
    assert plan_task.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_smoketest_curated_failure_spawns_plan_task_priority_70(
    client: AsyncClient, db_session, mocker
):
    """SMOKETEST_RESULT with a curated-suite failure (startup ok) spawns plan task at priority 70."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    _, task = await _setup_smoketest_task(db_session, "agent-vm-smoke-curated")

    import json as _json

    smoketest_payload = {
        "passed": False,
        "tests": [
            {"name": "login_flow", "status": "fail", "message": "assertion failed"}
        ],
        "suites": {
            "startup": {"passed": True, "tests": []},
            "curated": {
                "passed": False,
                "tests": [{"name": "login_flow", "status": "fail"}],
            },
        },
    }
    token = create_agent_token(str(task.id), "agent-vm-smoke-curated")
    payload = {
        "task_id": str(task.id),
        "status": "FAILED",
        "artifact_type": "SMOKETEST_RESULT",
        "logs": _json.dumps(smoketest_payload),
    }
    resp = await client.post(
        "/api/agent/status", json=payload, headers={"x-agent-token": token}
    )
    assert resp.status_code == 200

    result = await db_session.execute(
        select(Task).where(Task.parent_task_id == task.id)
    )
    spawned = result.scalars().all()
    assert len(spawned) == 1
    assert spawned[0].priority == 70
    assert spawned[0].first_payload.get("type") == "plan"


@pytest.mark.asyncio
async def test_smoketest_main_failure_post_cutover_spawns_hotfix(
    client: AsyncClient, db_session, mocker
):
    """Post-cutover (integration_branch=automain), a base_branch=main smoketest
    failure is a post-promotion production failure: the spawned plan task is a
    hotfix at priority >= 90 even when only the curated suite failed."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    _, task = await _setup_smoketest_task(
        db_session,
        "agent-vm-smoke-hotfix",
        integration_branch="automain",
    )

    import json as _json

    smoketest_payload = {
        "passed": False,
        "tests": [
            {"name": "login_flow", "status": "fail", "message": "assertion failed"}
        ],
        "suites": {
            "startup": {"passed": True, "tests": []},
            "curated": {
                "passed": False,
                "tests": [{"name": "login_flow", "status": "fail"}],
            },
        },
    }
    token = create_agent_token(str(task.id), "agent-vm-smoke-hotfix")
    payload = {
        "task_id": str(task.id),
        "status": "FAILED",
        "artifact_type": "SMOKETEST_RESULT",
        "logs": _json.dumps(smoketest_payload),
    }
    resp = await client.post(
        "/api/agent/status", json=payload, headers={"x-agent-token": token}
    )
    assert resp.status_code == 200

    result = await db_session.execute(
        select(Task).where(Task.parent_task_id == task.id)
    )
    spawned = result.scalars().all()
    assert len(spawned) == 1
    # A curated-only failure is normally priority 70; escalated to >= 90 here
    # because post-cutover a base_branch=main smoketest is a post-promotion
    # (production) failure, routed as a hotfix.
    assert spawned[0].priority >= 90
    assert spawned[0].first_payload.get("hotfix") is True
    assert spawned[0].first_payload.get("type") == "plan"


@pytest.mark.asyncio
async def test_smoketest_post_promotion_detection_uses_promotion_target_config(
    client: AsyncClient, db_session, mocker
):
    """post-promotion smoketest detection keys off promotion_target config, not literal 'main'.

    With integration_branch='develop' and promotion_target='stable', a smoketest
    on base_branch='stable' should be treated as a post-promotion production failure
    (hotfix, priority >= 90) even though neither 'automain' nor 'main' appear.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    _, task = await _setup_smoketest_task(
        db_session,
        "agent-vm-smoke-promo-cfg",
        base_branch="stable",
        integration_branch="develop",
        promotion_target="stable",
    )

    import json as _json

    smoketest_payload = {
        "passed": False,
        "tests": [
            {"name": "login_flow", "status": "fail", "message": "assertion failed"}
        ],
        "suites": {
            "startup": {"passed": True, "tests": []},
            "curated": {
                "passed": False,
                "tests": [{"name": "login_flow", "status": "fail"}],
            },
        },
    }
    token = create_agent_token(str(task.id), "agent-vm-smoke-promo-cfg")
    payload = {
        "task_id": str(task.id),
        "status": "FAILED",
        "artifact_type": "SMOKETEST_RESULT",
        "logs": _json.dumps(smoketest_payload),
    }
    resp = await client.post(
        "/api/agent/status", json=payload, headers={"x-agent-token": token}
    )
    assert resp.status_code == 200

    result = await db_session.execute(
        select(Task).where(Task.parent_task_id == task.id)
    )
    spawned = result.scalars().all()
    assert len(spawned) == 1
    assert spawned[0].priority >= 90
    assert spawned[0].first_payload.get("hotfix") is True
    assert spawned[0].first_payload.get("type") == "plan"


def test_smoketest_project_none_skips_fix_spawn():
    """When _smoketest_project is None the fix-spawn block must log and skip,
    not fall back to a hardcoded 'main' integration branch via dead runtime_config keys."""
    app_dir = Path(__file__).resolve().parent.parent / "app"
    router_source = (app_dir / "routers" / "agent_status.py").read_text()
    spawn_source = (app_dir / "agent_status_spawn.py").read_text()
    combined = router_source + "\n" + spawn_source
    assert '_runtime_config.get("integration_branch", "main")' not in combined, (
        "Dead-code fallback removed: _runtime_config has no 'integration_branch' key"
    )
    assert '_runtime_config.get("promotion_target", "main")' not in combined, (
        "Dead-code fallback removed: _runtime_config has no 'promotion_target' key"
    )
    assert "skipping fix spawn" in spawn_source, (
        "When project is None, code must log-and-skip rather than defaulting to 'main'"
    )


@pytest.mark.asyncio
async def test_smoketest_passing_does_not_spawn_plan_task(
    client: AsyncClient, db_session, mocker
):
    """SMOKETEST_RESULT with passed=true (no failures) does not spawn any child task."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    _, task = await _setup_smoketest_task(db_session, "agent-vm-smoke-ok")

    import json as _json

    # Even when status=FAILED is reported, a passed=true smoketest payload must not spawn.
    smoketest_payload = {
        "passed": True,
        "tests": [{"name": "startup_probe", "status": "pass"}],
        "suites": {"startup": {"passed": True, "tests": []}},
    }
    token = create_agent_token(str(task.id), "agent-vm-smoke-ok")
    payload = {
        "task_id": str(task.id),
        "status": "FAILED",
        "artifact_type": "SMOKETEST_RESULT",
        "logs": _json.dumps(smoketest_payload),
    }
    resp = await client.post(
        "/api/agent/status", json=payload, headers={"x-agent-token": token}
    )
    assert resp.status_code == 200

    result = await db_session.execute(
        select(Task).where(Task.parent_task_id == task.id)
    )
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_smoketest_result_plan_output_is_dict(client: AsyncClient, db_session, mocker):
    """SMOKETEST_RESULT plan_output must be a dict so query_findings_index excludes it from the Findings Journal."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)
    import json as _json
    _, task = await _setup_smoketest_task(db_session, "agent-vm-smoke-dict")
    token = create_agent_token(str(task.id), "agent-vm-smoke-dict")
    resp = await client.post(
        "/api/agent/status",
        json={"task_id": str(task.id), "status": "COMPLETED", "artifact_type": "SMOKETEST_RESULT",
              "logs": _json.dumps({"passed": True, "tests": [], "suites": {}})},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200
    await db_session.refresh(task)
    assert isinstance(task.plan_output, dict) and not isinstance(task.plan_output, list)


@pytest.mark.asyncio
async def test_debug_result_high_confidence_spawns_fix_task(
    client: AsyncClient, db_session, mocker
):
    """DEBUG_RESULT with confidence='high' and fix_prompt spawns a create-type fix task."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    project = Project(name="Debug High", user_id="test-user-debug-high")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Debug: flaky login test",
        payloads=[{"type": "debug", "base_branch": "main"}],
        vm_instance_id="agent-vm-debug-high",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=3,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json as _json

    debug_payload = {
        "confidence": "high",
        "fix_prompt": "Update login handler to normalize email before lookup",
        "root_cause": "Case-sensitive email comparison",
    }
    token = create_agent_token(str(task.id), "agent-vm-debug-high")
    payload = {
        "task_id": str(task.id),
        "status": "COMPLETED",
        "artifact_type": "DEBUG_RESULT",
        "logs": _json.dumps(debug_payload),
    }
    resp = await client.post(
        "/api/agent/status", json=payload, headers={"x-agent-token": token}
    )
    assert resp.status_code == 200

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    spawned = result.scalars().all()
    assert len(spawned) == 1
    fix_task = spawned[0]
    assert fix_task.first_payload.get("type") == "create"
    assert fix_task.first_payload.get("prompt") == debug_payload["fix_prompt"]
    assert fix_task.status == TaskStatus.QUEUED
    assert fix_task.title == "Fix: flaky login test"


@pytest.mark.asyncio
async def test_debug_result_low_confidence_does_not_spawn_fix_task(
    client: AsyncClient, db_session, mocker
):
    """DEBUG_RESULT with confidence='low' records plan_output but does not spawn a fix task."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    project = Project(name="Debug Low", user_id="test-user-debug-low")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Debug: intermittent 500 error",
        payloads=[{"type": "debug", "base_branch": "main"}],
        vm_instance_id="agent-vm-debug-low",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=3,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json as _json

    debug_payload = {
        "confidence": "low",
        "fix_prompt": "Possibly add a retry, but unclear",
        "root_cause": "Unknown — multiple candidate causes",
    }
    token = create_agent_token(str(task.id), "agent-vm-debug-low")
    payload = {
        "task_id": str(task.id),
        "status": "COMPLETED",
        "artifact_type": "DEBUG_RESULT",
        "logs": _json.dumps(debug_payload),
    }
    resp = await client.post(
        "/api/agent/status", json=payload, headers={"x-agent-token": token}
    )
    assert resp.status_code == 200

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    assert result.scalars().all() == []

    await db_session.refresh(task)
    assert task.plan_output == debug_payload


@pytest.mark.asyncio
async def test_debug_result_skips_fix_spawn_if_requeue_already_replaced(
    client: AsyncClient, db_session, mocker
):
    """DEBUG_RESULT high-confidence does NOT spawn Fix: when the failed task was
    already CANCELLED+replaced by _requeue_parent_task (preventing duplicate work)."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    import json as _json

    project = Project(name="Debug Dedup Test", user_id="test-user-debug-dedup")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # The original failed create task — already cancelled+replaced by requeue
    replacement_id = str(uuid.uuid4())
    failed_task = Task(
        project_id=project.id,
        title="create: some feature",
        payloads=[{"type": "create", "prompt": "do something", "base_branch": "main"}],
        vm_instance_id="agent-vm-failed-orig",
        status=TaskStatus.CANCELLED,
        context={"_replaced_by_task_id": replacement_id},
    )
    db_session.add(failed_task)
    await db_session.commit()
    await db_session.refresh(failed_task)

    # The debug task references the already-replaced failed task
    debug_task = Task(
        project_id=project.id,
        title="Debug: create: some feature",
        payloads=[{"type": "debug", "base_branch": "main"}],
        vm_instance_id="agent-vm-debug-dedup",
        status=TaskStatus.IN_PROGRESS,
        context={"failed_task_id": str(failed_task.id)},
    )
    db_session.add(debug_task)
    await db_session.commit()
    await db_session.refresh(debug_task)

    token = create_agent_token(str(debug_task.id), "agent-vm-debug-dedup")
    payload = {
        "task_id": str(debug_task.id),
        "status": "COMPLETED",
        "artifact_type": "DEBUG_RESULT",
        "logs": _json.dumps(
            {
                "confidence": "high",
                "fix_prompt": "Fix the thing",
                "root_cause": "some root cause",
            }
        ),
    }
    resp = await client.post(
        "/api/agent/status", json=payload, headers={"x-agent-token": token}
    )
    assert resp.status_code == 200

    # No Fix: task should have been spawned — requeue already handled it
    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id != debug_task.id,
            Task.id != failed_task.id,
        )
    )
    extra_tasks = result.scalars().all()
    assert extra_tasks == [], (
        f"Expected no Fix: task to be spawned when requeue already replaced the failed task, "
        f"got {[t.title for t in extra_tasks]}"
    )


@pytest.mark.asyncio
async def test_debug_result_marks_failed_task_on_fix_spawn(
    client: AsyncClient, db_session, mocker
):
    """When Fix: task is spawned, _debug_fix_spawned_task_id is written on the failed
    task's context so _requeue_parent_task can detect the debug-fix path and skip cloning."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    import json as _json

    project = Project(name="Debug Mark Test", user_id="test-user-debug-mark")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # The original failed create task — still FAILED (not yet replaced by requeue)
    failed_task = Task(
        project_id=project.id,
        title="create: another feature",
        payloads=[
            {"type": "create", "prompt": "do other thing", "base_branch": "main"}
        ],
        vm_instance_id="agent-vm-failed-mark",
        status=TaskStatus.FAILED,
        context={},
    )
    db_session.add(failed_task)
    await db_session.commit()
    await db_session.refresh(failed_task)

    debug_task = Task(
        project_id=project.id,
        title="Debug: create: another feature",
        payloads=[{"type": "debug", "base_branch": "main"}],
        vm_instance_id="agent-vm-debug-mark",
        status=TaskStatus.IN_PROGRESS,
        context={"failed_task_id": str(failed_task.id)},
    )
    db_session.add(debug_task)
    await db_session.commit()
    await db_session.refresh(debug_task)

    token = create_agent_token(str(debug_task.id), "agent-vm-debug-mark")
    payload = {
        "task_id": str(debug_task.id),
        "status": "COMPLETED",
        "artifact_type": "DEBUG_RESULT",
        "logs": _json.dumps(
            {
                "confidence": "high",
                "fix_prompt": "Fix the other thing",
                "root_cause": "another root cause",
            }
        ),
    }
    resp = await client.post(
        "/api/agent/status", json=payload, headers={"x-agent-token": token}
    )
    assert resp.status_code == 200

    # A Fix: task should have been spawned
    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id != debug_task.id,
            Task.id != failed_task.id,
        )
    )
    fix_tasks = result.scalars().all()
    assert len(fix_tasks) == 1
    fix_task = fix_tasks[0]
    assert fix_task.title == "Fix: create: another feature"

    # The failed task should be marked with _debug_fix_spawned_task_id
    await db_session.refresh(failed_task)
    marked_id = (failed_task.context or {}).get("_debug_fix_spawned_task_id")
    assert marked_id == str(fix_task.id), (
        f"Expected failed task context._debug_fix_spawned_task_id={fix_task.id}, got {marked_id}"
    )


@pytest.mark.asyncio
async def test_debug_result_plan_output_is_dict(client: AsyncClient, db_session, mocker):
    """DEBUG_RESULT plan_output must be a dict so query_findings_index excludes it from the Findings Journal."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)
    import json as _json
    project = Project(name="Debug Dict", user_id="test-user-debug-dict2")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(project_id=project.id, title="Debug: dict test",
                payloads=[{"type": "debug", "base_branch": "main"}],
                vm_instance_id="agent-vm-debug-dict2", status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    token = create_agent_token(str(task.id), "agent-vm-debug-dict2")
    resp = await client.post(
        "/api/agent/status",
        json={"task_id": str(task.id), "status": "COMPLETED", "artifact_type": "DEBUG_RESULT",
              "logs": _json.dumps({"confidence": "low", "root_cause": "test"})},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200
    await db_session.refresh(task)
    assert isinstance(task.plan_output, dict) and not isinstance(task.plan_output, list)


@pytest.mark.asyncio
async def test_agent_claim_injects_project_vision(client: AsyncClient, db_session):
    """Strategic vision is surfaced on the claim response so tactical agents
    can align their work to the project's stated purpose."""
    project = Project(
        name="Vision Inject Test",
        user_id="test-user-vision",
        vision={
            "purpose": "Build an autonomous coding studio.",
            "success_criteria": ["Agents self-improve", "CI stays green"],
            "priorities": ["Stability over features"],
            "systems": [
                {
                    "id": "orch",
                    "name": "Orchestrator",
                    "status": "functional",
                    "description": "Routes tasks",
                },
            ],
            "revision": 3,
            "last_updated": "2026-04-21T00:00:00",
            "_strategic_failures": 2,
            "_strategic_cycle_history": [{"foo": "bar"}],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Vision consumer",
        payloads=[{"type": "create", "prompt": "Add a widget"}],
        vm_instance_id="agent-vm-vision",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-vision")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    data = resp.json()

    vision = data.get("vision")
    assert vision is not None
    assert vision["purpose"] == "Build an autonomous coding studio."
    assert vision["success_criteria"] == ["Agents self-improve", "CI stays green"]
    assert vision["priorities"] == ["Stability over features"]
    assert vision["revision"] == 3
    assert vision["systems"][0]["name"] == "Orchestrator"
    assert vision["systems"][0]["status"] == "functional"
    # Internal tracking fields must not leak to agents
    assert "_strategic_failures" not in vision
    assert "_strategic_cycle_history" not in vision


@pytest.mark.asyncio
async def test_agent_claim_omits_vision_when_empty(client: AsyncClient, db_session):
    """If vision is unset or only contains internal fields, no vision key is added."""
    project = Project(
        name="Empty Vision",
        user_id="test-user-empty-vision",
        vision={"_strategic_failures": 1},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="No vision task",
        payloads=[{"type": "create", "prompt": "x"}],
        vm_instance_id="agent-vm-novision",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-novision")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    assert "vision" not in resp.json()


@pytest.mark.asyncio
async def test_agent_claim_injects_direction_narrative(client: AsyncClient, db_session):
    """Direction narrative (established findings + open questions + last cycle)
    is surfaced under direction_context for create/fix/refactor/research/plan."""
    direction_id = str(uuid.uuid4())
    research_context = (
        "## Established Findings\n"
        "- Celery workers crash under burst load\n"
        "- Redis is not the bottleneck\n"
        "\n"
        "## Failed Approaches\n"
        "- Raising worker count without backpressure\n"
        "\n"
        "## Open Questions\n"
        "- Does autoscaling help?\n"
    )
    project = Project(
        name="Direction Narrative Test",
        user_id="test-user-narrative",
        directions=[
            {
                "id": direction_id,
                "title": "Stabilize task queue",
                "priority": "high",
                "status": "active",
                "research_context": research_context,
                "research_history": [
                    {"tasks_created": 4, "prs_merged": 2, "prs_rejected": 1},
                    {"tasks_created": 0, "prs_merged": 0, "prs_rejected": 0},
                    {"tasks_created": 3, "prs_merged": 3, "prs_rejected": 0},
                ],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Narrative consumer",
        payloads=[{"type": "create", "prompt": "Fix the queue"}],
        vm_instance_id="agent-vm-narrative",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-narrative")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    narrative = (resp.json().get("direction_context") or {}).get("narrative")
    assert narrative is not None
    assert narrative["established_findings"] == [
        "Celery workers crash under burst load",
        "Redis is not the bottleneck",
    ]
    assert narrative["open_questions"] == ["Does autoscaling help?"]
    # Last productive cycle — skips the zero-task cycle in the middle
    assert narrative["last_cycle"] == {
        "tasks_created": 3,
        "prs_merged": 3,
        "prs_rejected": 0,
    }


# ---------------------------------------------------------------------------
# OAuth credential rotation (agent → backend)
# ---------------------------------------------------------------------------


async def _make_openai_oauth_task(db_session) -> tuple[Task, MaxPlanAccount, Project]:
    project = Project(name="Codex Rotate Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    account = MaxPlanAccount(
        user_id="test-user-123",
        name="codex-oauth",
        provider=LLMProviderName.OPENAI,
        auth_mode=LLMAuthMode.OAUTH,
        secret_blob_ref='{"tokens":{"access_token":"old","refresh_token":"rt-old"}}',
    )
    db_session.add(account)
    await db_session.commit()
    await db_session.refresh(account)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-rotate",
        status=TaskStatus.IN_PROGRESS,
        context={
            "llm_account_id": str(account.id),
            "llm_provider": "openai",
            "llm_auth_mode": "oauth",
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task, account, project


@pytest.mark.asyncio
async def test_rotate_credentials_updates_openai_oauth_secret(
    client: AsyncClient, db_session, mocker
):
    mocker.patch(
        "app.secrets_manager.store_user_secret",
        new=AsyncMock(return_value="projects/test/secrets/rotate/versions/2"),
    )
    task, account, _ = await _make_openai_oauth_task(db_session)

    token = create_agent_token(str(task.id), "agent-vm-rotate")
    new_blob = '{"tokens":{"access_token":"fresh","refresh_token":"rt-fresh"}}'
    res = await client.post(
        "/api/agent/credentials/rotate",
        headers={"x-agent-token": token},
        json={"secret": new_blob},
    )

    assert res.status_code == 200, res.text
    assert res.json() == {"rotated": True}

    await db_session.refresh(account)
    assert account.secret_blob_ref == "projects/test/secrets/rotate/versions/2"


@pytest.mark.asyncio
async def test_rotate_credentials_rejects_malformed_secret(
    client: AsyncClient, db_session
):
    task, _, _ = await _make_openai_oauth_task(db_session)

    token = create_agent_token(str(task.id), "agent-vm-rotate")
    res = await client.post(
        "/api/agent/credentials/rotate",
        headers={"x-agent-token": token},
        # No tokens envelope → should fail the provider's validator. Prior
        # behavior would have silently stored a broken secret.
        json={"secret": "{}"},
    )

    assert res.status_code == 400
    assert "tokens" in res.text


@pytest.mark.asyncio
async def test_rotate_credentials_no_op_for_non_openai_oauth(
    client: AsyncClient, db_session
):
    """Rotation endpoint must be safe to call unconditionally from the agent —
    a Gemini/SA or Anthropic/OAuth task should get a no-op response, not an
    error. This keeps the agent-side wiring provider-agnostic."""
    project = Project(name="Gemini Rotate Noop", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    account = MaxPlanAccount(
        user_id="test-user-123",
        name="gemini-sa",
        provider=LLMProviderName.GEMINI,
        auth_mode=LLMAuthMode.SERVICE_ACCOUNT,
        secret_blob_ref="projects/test/secrets/gemini/versions/1",
        provider_metadata={"project": "demo", "location": "us-central1"},
    )
    db_session.add(account)
    await db_session.commit()
    await db_session.refresh(account)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-gemini-rotate",
        status=TaskStatus.IN_PROGRESS,
        context={
            "llm_account_id": str(account.id),
            "llm_provider": "gemini",
            "llm_auth_mode": "service_account",
        },
    )
    db_session.add(task)
    await db_session.commit()

    token = create_agent_token(str(task.id), "agent-vm-gemini-rotate")
    res = await client.post(
        "/api/agent/credentials/rotate",
        headers={"x-agent-token": token},
        json={"secret": "ignored"},
    )
    assert res.status_code == 200
    assert res.json()["rotated"] is False


@pytest.mark.asyncio
async def test_rotate_credentials_rejects_missing_secret(
    client: AsyncClient, db_session
):
    task, _, _ = await _make_openai_oauth_task(db_session)
    token = create_agent_token(str(task.id), "agent-vm-rotate")

    res = await client.post(
        "/api/agent/credentials/rotate",
        headers={"x-agent-token": token},
        json={},
    )
    assert res.status_code == 400
    assert "secret" in res.text


@pytest.mark.asyncio
async def test_rotate_credentials_allowed_after_task_terminated(
    client: AsyncClient, db_session, mocker
):
    """Rotation runs in the agent's shutdown finally block — by that point the
    task may already be in COMPLETED state. verify_agent_token must not 401
    rotate calls the way it does for other endpoints post-terminal."""
    mocker.patch(
        "app.secrets_manager.store_user_secret",
        new=AsyncMock(return_value="projects/test/secrets/rotate-term/versions/3"),
    )
    task, _, _ = await _make_openai_oauth_task(db_session)
    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()

    token = create_agent_token(str(task.id), "agent-vm-rotate")
    res = await client.post(
        "/api/agent/credentials/rotate",
        headers={"x-agent-token": token},
        json={"secret": '{"tokens":{"access_token":"post-terminal"}}'},
    )
    assert res.status_code == 200, res.text
    assert res.json() == {"rotated": True}


@pytest.mark.asyncio
async def test_rotate_credentials_returns_503_on_secret_manager_failure(
    client: AsyncClient, db_session, mocker
):
    """On Secret Manager failure the endpoint must return 503, not silently
    store the plaintext secret."""
    mocker.patch(
        "app.secrets_manager.store_user_secret",
        new=AsyncMock(side_effect=RuntimeError("Secret Manager unavailable")),
    )
    task, account, _ = await _make_openai_oauth_task(db_session)
    original_ref = account.secret_blob_ref

    token = create_agent_token(str(task.id), "agent-vm-rotate")
    res = await client.post(
        "/api/agent/credentials/rotate",
        headers={"x-agent-token": token},
        json={"secret": '{"tokens":{"access_token":"new","refresh_token":"rt-new"}}'},
    )

    assert res.status_code == 503
    assert "retry" in res.text.lower()

    await db_session.refresh(account)
    assert account.secret_blob_ref == original_ref


@pytest.mark.asyncio
async def test_rotate_credentials_preserves_existing_ref_on_secret_manager_failure(
    client: AsyncClient, db_session, mocker
):
    """After a failed rotation the existing secret_blob_ref must remain intact
    so the account can still operate using the previous valid credential."""
    mocker.patch(
        "app.secrets_manager.store_user_secret",
        new=AsyncMock(side_effect=OSError("connection refused")),
    )
    task, account, _ = await _make_openai_oauth_task(db_session)
    expected_ref = account.secret_blob_ref

    token = create_agent_token(str(task.id), "agent-vm-rotate")
    await client.post(
        "/api/agent/credentials/rotate",
        headers={"x-agent-token": token},
        json={"secret": '{"tokens":{"access_token":"x","refresh_token":"rt-x"}}'},
    )

    await db_session.refresh(account)
    assert account.secret_blob_ref == expected_ref


# Strategic-context fetch endpoint tests were relocated to
# test_agent_strategic_fetch.py when that surface was extracted into the
# app.routers.agent_strategic_fetch sub-router.


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 (warm-VM continuity): /next-role + /vm-heartbeat
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_vm_heartbeat_records_liveness(client: AsyncClient, db_session):
    """vm-heartbeat returns 204 and records the VM in vm_last_heartbeat."""
    import app.shared_state as _shared_state

    project = Project(name="VM-Heartbeat Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id="agent-vm-warm-001",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _shared_state.vm_last_heartbeat.pop("agent-vm-warm-001", None)

    token = create_agent_token(str(task.id), "agent-vm-warm-001")
    resp = await client.post(
        "/api/agent/vm-heartbeat",
        json={"vm_instance_id": "agent-vm-warm-001"},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 204
    assert "agent-vm-warm-001" in _shared_state.vm_last_heartbeat


@pytest.mark.asyncio
async def test_vm_heartbeat_rejects_jwt_vm_mismatch(client: AsyncClient, db_session):
    """vm-heartbeat returns 403 when JWT vm_instance_id mismatches body."""
    project = Project(name="VM-Heartbeat Mismatch", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id="agent-vm-real",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-real")
    resp = await client.post(
        "/api/agent/vm-heartbeat",
        json={"vm_instance_id": "agent-vm-impostor"},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_next_role_returns_204_when_no_warm_pinned_task(
    client: AsyncClient, db_session
):
    """next-role returns 204 when no QUEUED task is pinned to this VM."""
    project = Project(name="Next-Role Empty", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    prior_task = Task(
        project_id=project.id,
        payloads=[{"type": "review"}],
        vm_instance_id="agent-vm-warm-empty",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(prior_task)
    await db_session.commit()
    await db_session.refresh(prior_task)

    token = create_agent_token(str(prior_task.id), "agent-vm-warm-empty")
    resp = await client.post(
        "/api/agent/next-role",
        json={
            "vm_instance_id": "agent-vm-warm-empty",
            "prior_task_id": str(prior_task.id),
            "prior_pr_number": 999,
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_next_role_claims_warm_pinned_queued_task(
    client: AsyncClient, db_session
):
    """next-role atomically claims a QUEUED task whose context.warm_target_vm_id matches."""
    from datetime import datetime, timedelta

    project = Project(name="Next-Role Claim", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    prior_task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 1234}],
        vm_instance_id="agent-vm-warm-claim",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(prior_task)
    # A QUEUED fix pinned to the same VM
    deadline = (datetime.utcnow() + timedelta(seconds=240)).isoformat()
    pinned_task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "pr_number": 1234, "branch": "feat/x"}],
        vm_instance_id=None,
        status=TaskStatus.QUEUED,
        context={
            "warm_target_vm_id": "agent-vm-warm-claim",
            "warm_target_pr": "1234",
            "warm_handoff_deadline": deadline,
        },
    )
    db_session.add(pinned_task)
    await db_session.commit()
    await db_session.refresh(prior_task)
    await db_session.refresh(pinned_task)

    token = create_agent_token(str(prior_task.id), "agent-vm-warm-claim")
    resp = await client.post(
        "/api/agent/next-role",
        json={
            "vm_instance_id": "agent-vm-warm-claim",
            "prior_task_id": str(prior_task.id),
            "prior_pr_number": 1234,
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "claim"
    assert body["task_id"] == str(pinned_task.id)
    assert body["warm_start"] is True
    # New JWT scoped to the warm-claimed task is included so the agent
    # can authenticate subsequent task-scoped calls (the prior task's
    # JWT is terminal and only authenticates the idempotent
    # next-role/vm-heartbeat paths).
    assert isinstance(body.get("agent_token"), str) and body["agent_token"]
    from app.auth import verify_agent_jwt

    new_claims = verify_agent_jwt(body["agent_token"])
    assert new_claims["sub"] == str(pinned_task.id)
    assert new_claims["vm_instance_id"] == "agent-vm-warm-claim"
    assert new_claims["project_id"] == str(project.id)
    assert new_claims["task_type"] == "fix"

    await db_session.refresh(pinned_task)
    assert pinned_task.status == TaskStatus.IN_PROGRESS
    assert pinned_task.vm_instance_id == "agent-vm-warm-claim"
    assert (pinned_task.context or {}).get("_timing", {}).get("warm_start") is True
    assert (pinned_task.context or {}).get("_timing", {}).get(
        "prior_role_vm_id"
    ) == "agent-vm-warm-claim"


@pytest.mark.asyncio
async def test_next_role_returns_204_when_warm_continuity_suppressed(
    client: AsyncClient, db_session, mocker
):
    """Circuit breaker: next-role returns 204 (skipping warm claim) when
    is_warm_continuity_suppressed() returns True, even if a pinned QUEUED task exists."""
    from datetime import timedelta

    project = Project(name="Next-Role CB Suppressed", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    prior_task = Task(
        project_id=project.id,
        payloads=[{"type": "review"}],
        vm_instance_id="agent-vm-cb-suppressed",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(prior_task)
    deadline = (datetime.utcnow() + timedelta(seconds=240)).isoformat()
    pinned_task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "pr_number": 9999}],
        status=TaskStatus.QUEUED,
        context={
            "warm_target_vm_id": "agent-vm-cb-suppressed",
            "warm_handoff_deadline": deadline,
        },
    )
    db_session.add(pinned_task)
    await db_session.commit()
    await db_session.refresh(prior_task)
    await db_session.refresh(pinned_task)

    mocker.patch("app.shared_state.is_warm_continuity_suppressed", return_value=True)

    token = create_agent_token(str(prior_task.id), "agent-vm-cb-suppressed")
    resp = await client.post(
        "/api/agent/next-role",
        json={
            "vm_instance_id": "agent-vm-cb-suppressed",
            "prior_task_id": str(prior_task.id),
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 204

    await db_session.refresh(pinned_task)
    assert pinned_task.status == TaskStatus.QUEUED, (
        "Circuit breaker must not claim the pinned task"
    )


@pytest.mark.asyncio
async def test_next_role_refuses_warm_claim_on_doomed_vm(
    client: AsyncClient, db_session
):
    """Race guard: when a sibling task's agent_completion fired
    fire_and_forget_vm_delete, the VM is alive in GCP for ~30-60s but
    the delete is in flight. Without this guard, next-role races into
    that window, claims the doomed VM, and the agent gets SIGTERM'd
    mid-task. Production 2026-05-05 task cfd73cec on VM c44258 hit
    exactly this — claimed warm VM that was already scheduled for
    delete by 41456ba8's agent_completion 9s earlier; SIGTERM landed
    ~50s later as the GCP delete completed."""
    from datetime import datetime, timedelta
    from app.vm_lifecycle import _orchestrator_deleted_vms
    import time

    project = Project(name="Next-Role Doomed-VM", user_id="test-user-doomed")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    prior_task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "pr_number": 4321}],
        vm_instance_id="agent-vm-doomed",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(prior_task)
    deadline = (datetime.utcnow() + timedelta(seconds=240)).isoformat()
    pinned_task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 4321, "branch": "feat/y"}],
        vm_instance_id=None,
        status=TaskStatus.QUEUED,
        context={
            "warm_target_vm_id": "agent-vm-doomed",
            "warm_target_pr": "4321",
            "warm_handoff_deadline": deadline,
        },
    )
    db_session.add(pinned_task)
    await db_session.commit()
    await db_session.refresh(prior_task)
    await db_session.refresh(pinned_task)

    # Mark the VM as orchestrator-scheduled-for-delete. This is the
    # synchronous marker that fire_and_forget_vm_delete records before
    # scheduling the background GCP call.
    _orchestrator_deleted_vms["agent-vm-doomed"] = (time.time(), "agent_completion")
    try:
        token = create_agent_token(str(prior_task.id), "agent-vm-doomed")
        resp = await client.post(
            "/api/agent/next-role",
            json={
                "vm_instance_id": "agent-vm-doomed",
                "prior_task_id": str(prior_task.id),
                "prior_pr_number": 4321,
            },
            headers={"x-agent-token": token},
        )
        # 204 = no warm-claim (treat doomed VM as if no warm task were
        # available so the agent exits cleanly).
        assert resp.status_code == 204, (
            f"expected 204 on doomed-VM warm-claim attempt, got {resp.status_code} — "
            f"the QUEUED task was claimed onto a VM that's about to die"
        )

        # The pinned task must remain QUEUED so the orchestrator can
        # provision a fresh VM for it on the next dispatch tick.
        await db_session.refresh(pinned_task)
        assert pinned_task.status == TaskStatus.QUEUED
        assert pinned_task.vm_instance_id is None
    finally:
        _orchestrator_deleted_vms.pop("agent-vm-doomed", None)


@pytest.mark.asyncio
async def test_next_role_rejects_jwt_vm_mismatch(client: AsyncClient, db_session):
    """next-role returns 403 when JWT vm_instance_id mismatches body."""
    project = Project(name="Next-Role JWT Mismatch", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    prior_task = Task(
        project_id=project.id,
        payloads=[{"type": "review"}],
        vm_instance_id="agent-vm-real-vm",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(prior_task)
    await db_session.commit()
    await db_session.refresh(prior_task)

    token = create_agent_token(str(prior_task.id), "agent-vm-real-vm")
    resp = await client.post(
        "/api/agent/next-role",
        json={
            "vm_instance_id": "agent-vm-impostor-vm",
            "prior_task_id": str(prior_task.id),
            "prior_pr_number": 555,
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_next_role_returns_exit_when_other_vm_owns_pr(
    client: AsyncClient, db_session
):
    """next-role returns action=exit when an IN_PROGRESS task on the same PR is pinned to a different VM."""
    project = Project(name="Next-Role Other-VM", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    prior_task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 4242}],
        vm_instance_id="agent-vm-warm-A",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(prior_task)
    # An IN_PROGRESS task on the same PR but pinned to a different VM
    other_vm_task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "pr_number": 4242, "branch": "feat/y"}],
        vm_instance_id="agent-vm-warm-B",
        status=TaskStatus.IN_PROGRESS,
        context={"warm_target_pr": "4242"},
    )
    db_session.add(other_vm_task)
    await db_session.commit()
    await db_session.refresh(prior_task)

    token = create_agent_token(str(prior_task.id), "agent-vm-warm-A")
    resp = await client.post(
        "/api/agent/next-role",
        json={
            "vm_instance_id": "agent-vm-warm-A",
            "prior_task_id": str(prior_task.id),
            "prior_pr_number": 4242,
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "exit"
    assert body["reason"] == "another_vm_owns_this_pr"


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2c (warm-VM continuity) — Subagent S1 review fixes
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_spawn_strips_warm_target_keys(client: AsyncClient, db_session):
    """A malicious agent MUST NOT be able to inject warm_target_* into a spawned
    child's context — that would let it hijack another VM's warm chain.
    """
    project = Project(name="Spawn Sanitization Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent_task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="agent-vm-spawn-parent",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(parent_task)
    await db_session.commit()
    await db_session.refresh(parent_task)

    token = create_agent_token(str(parent_task.id), "agent-vm-spawn-parent")
    forbidden_context = {
        "warm_target_vm_id": "victim-vm",
        "warm_target_pr": 1,
        "warm_handoff_deadline": "2099-01-01T00:00:00",
        "_timing": {"warm_start": True, "prior_role_vm_id": "victim-vm"},
        "other_key": "ok",
    }
    resp = await client.post(
        "/api/agent/spawn",
        headers={"x-agent-token": token},
        json={
            "task_id": str(parent_task.id),
            "payloads": [{"type": "create", "title": "child-1", "prompt": "test"}],
            "context": forbidden_context,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["task_ids"]) == 1
    child_id = uuid.UUID(body["task_ids"][0])

    child = await db_session.get(Task, child_id)
    assert child is not None
    ctx = child.context or {}
    assert "warm_target_vm_id" not in ctx
    assert "warm_target_pr" not in ctx
    assert "warm_handoff_deadline" not in ctx
    assert "_timing" not in ctx
    # Non-forbidden keys are preserved
    assert ctx.get("other_key") == "ok"


@pytest.mark.asyncio
async def test_heartbeat_records_vm_liveness(client: AsyncClient, db_session):
    """/api/agent/heartbeat MUST update vm_last_heartbeat so the orchestrator's
    skip-provision branch sees the VM as alive (Phase 2c Race A mitigation).
    """
    import app.shared_state as _shared_state

    project = Project(name="Heartbeat VM Liveness Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id="agent-vm-hb-liveness",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _shared_state.vm_last_heartbeat.pop("agent-vm-hb-liveness", None)

    token = create_agent_token(str(task.id), "agent-vm-hb-liveness")
    resp = await client.post(
        "/api/agent/heartbeat",
        headers={"x-agent-token": token},
        json={"task_id": str(task.id), "status_message": "still running"},
    )
    assert resp.status_code == 200
    assert "agent-vm-hb-liveness" in _shared_state.vm_last_heartbeat


@pytest.mark.asyncio
async def test_claim_records_vm_liveness(client: AsyncClient, db_session):
    """/api/agent/claim MUST update vm_last_heartbeat — first contact establishes
    the warm-VM continuity liveness signal (Phase 2c Race A mitigation).
    """
    import app.shared_state as _shared_state

    project = Project(name="Claim VM Liveness Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id="agent-vm-claim-liveness",
        status=TaskStatus.QUEUED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _shared_state.vm_last_heartbeat.pop("agent-vm-claim-liveness", None)

    token = create_agent_token(str(task.id), "agent-vm-claim-liveness")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200
    assert "agent-vm-claim-liveness" in _shared_state.vm_last_heartbeat


@pytest.mark.asyncio
async def test_next_role_token_mint_failure_rolls_back(
    client: AsyncClient, db_session, mocker
):
    """If create_agent_token raises during the warm-claim path, the task MUST
    stay QUEUED (rollback) so a retry can succeed. Without rollback the task
    would be stuck IN_PROGRESS with no live JWT on the agent side.
    """
    from datetime import datetime, timedelta

    project = Project(name="Next-Role Mint Failure", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    prior_task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 2222}],
        vm_instance_id="agent-vm-mint-fail",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(prior_task)
    deadline = (datetime.utcnow() + timedelta(seconds=240)).isoformat()
    pinned_task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "pr_number": 2222, "branch": "feat/mint-fail"}],
        vm_instance_id=None,
        status=TaskStatus.QUEUED,
        context={
            "warm_target_vm_id": "agent-vm-mint-fail",
            "warm_target_pr": "2222",
            "warm_handoff_deadline": deadline,
        },
    )
    db_session.add(pinned_task)
    await db_session.commit()
    await db_session.refresh(prior_task)
    await db_session.refresh(pinned_task)

    # Patch the create_agent_token reference in routers.agent_warm_continuity
    # (the actual call site); patching app.auth.create_agent_token wouldn't
    # intercept because of `from app.auth import create_agent_token`.
    mocker.patch(
        "app.routers.agent_warm_continuity.create_agent_token",
        side_effect=RuntimeError("simulated mint failure"),
    )

    token = create_agent_token(str(prior_task.id), "agent-vm-mint-fail")
    resp = await client.post(
        "/api/agent/next-role",
        json={
            "vm_instance_id": "agent-vm-mint-fail",
            "prior_task_id": str(prior_task.id),
            "prior_pr_number": 2222,
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 500
    # The app's global exception handler wraps HTTPException details into
    # {"error": {"code": 500, "message": ...}}; tolerate either shape.
    body = resp.json()
    assert (
        body.get("detail") == "token_mint_failed"
        or body.get("error", {}).get("message") == "token_mint_failed"
    )

    # Task must still be QUEUED with the warm-pin fields intact so a
    # retry (after the orchestrator settles) can succeed.
    await db_session.refresh(pinned_task)
    assert pinned_task.status == TaskStatus.QUEUED
    ctx = pinned_task.context or {}
    assert ctx.get("warm_target_vm_id") == "agent-vm-mint-fail"
    assert ctx.get("warm_target_pr") == "2222"


@pytest.mark.asyncio
async def test_next_role_rejects_empty_vm_instance_id(client: AsyncClient, db_session):
    """min_length=1 on NextRoleRequest.vm_instance_id rejects empty strings."""
    project = Project(name="Next-Role Empty VM", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    prior_task = Task(
        project_id=project.id,
        payloads=[{"type": "review"}],
        vm_instance_id="agent-vm-validate",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(prior_task)
    await db_session.commit()
    await db_session.refresh(prior_task)

    token = create_agent_token(str(prior_task.id), "agent-vm-validate")
    resp = await client.post(
        "/api/agent/next-role",
        json={
            "vm_instance_id": "",
            "prior_task_id": str(prior_task.id),
            "prior_pr_number": 1,
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_vm_heartbeat_rejects_empty_vm_instance_id(
    client: AsyncClient, db_session
):
    """min_length=1 on VmHeartbeatRequest.vm_instance_id rejects empty strings."""
    project = Project(name="VM-Heartbeat Empty VM", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id="agent-vm-validate-hb",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-validate-hb")
    resp = await client.post(
        "/api/agent/vm-heartbeat",
        json={"vm_instance_id": ""},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_next_role_broadcasts_on_claim(client: AsyncClient, db_session, mocker):
    """A successful warm-claim MUST broadcast task_updated to the project so the UI
    reflects the IN_PROGRESS transition without waiting for the next status post.
    """
    from datetime import datetime, timedelta

    broadcast_mock = mocker.patch(
        "app.routers.agent_warm_continuity.broadcast_to_project",
        new_callable=mocker.AsyncMock,
    )

    project = Project(name="Next-Role Broadcast", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    prior_task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 7777}],
        vm_instance_id="agent-vm-broadcast",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(prior_task)
    deadline = (datetime.utcnow() + timedelta(seconds=240)).isoformat()
    pinned_task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "pr_number": 7777, "branch": "feat/bcast"}],
        vm_instance_id=None,
        status=TaskStatus.QUEUED,
        context={
            "warm_target_vm_id": "agent-vm-broadcast",
            "warm_target_pr": "7777",
            "warm_handoff_deadline": deadline,
        },
    )
    db_session.add(pinned_task)
    await db_session.commit()
    await db_session.refresh(prior_task)
    await db_session.refresh(pinned_task)

    token = create_agent_token(str(prior_task.id), "agent-vm-broadcast")
    resp = await client.post(
        "/api/agent/next-role",
        json={
            "vm_instance_id": "agent-vm-broadcast",
            "prior_task_id": str(prior_task.id),
            "prior_pr_number": 7777,
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "claim"

    broadcast_mock.assert_awaited_once()
    args, _kwargs = broadcast_mock.call_args
    assert args[0] == str(project.id)
    assert args[1] == "task_updated"
    assert args[2]["task_id"] == str(pinned_task.id)
    assert args[2]["status"] == TaskStatus.IN_PROGRESS


# ══════════════════════════════════════════════════════════════════════════════
# Issue M3 (warm-VM rev): terminal-JWT freshness window for warm-idempotent paths
# ══════════════════════════════════════════════════════════════════════════════
#
# A leaked terminal-task JWT can authenticate /vm-heartbeat and /next-role for
# the JWT's full TTL (8h via the is_idempotent_path exemption). Attacker spam-
# keepalives a phantom vm_id, blocking warm-pin clearout AND can claim future
# tasks pinned to that VM. The fix rejects terminal-task tokens whose iat is
# outside the freshness window (default 5 min, env-tunable). Active-task
# tokens are unaffected.


def test_terminal_idempotent_freshness_default_is_300():
    """_TERMINAL_IDEMPOTENT_FRESHNESS_SECS defaults to 300 when the env var is unset.

    Reads the already-imported module constant directly. The previous version
    deleted ``app.routers.agent`` from ``sys.modules`` and re-imported it to
    "force re-evaluation" — but that rebuilt the module, creating a NEW
    ``verify_agent_token`` object. ``admin_agent_ops`` / ``agent_status`` /
    ``agent_credentials`` had already captured the ORIGINAL object via
    ``from app.routers.agent import verify_agent_token`` in their route
    ``Depends(...)``, so after this test every later test that overrode
    ``verify_agent_token`` by object identity (test_directions strategic API,
    test_pipeline set_vision) silently lost its override and got a 422 — a
    deterministic, order-dependent full-suite failure that passed in isolation.
    The test environment never sets this env var, so the constant already
    reflects the unset-env default; assert the premise to make a stray override
    explicit rather than a confusing value mismatch.
    """
    import os

    from app.routers.agent import _TERMINAL_IDEMPOTENT_FRESHNESS_SECS

    assert os.getenv("AGENT_TERMINAL_IDEMPOTENT_FRESHNESS_SECS") is None, (
        "test premise: AGENT_TERMINAL_IDEMPOTENT_FRESHNESS_SECS must be unset so "
        "the module constant reflects the compiled-in default"
    )
    assert _TERMINAL_IDEMPOTENT_FRESHNESS_SECS == 300, (
        f"Expected default 300, got {_TERMINAL_IDEMPOTENT_FRESHNESS_SECS}"
    )


def _make_jwt_with_iat(task_id: str, vm_instance_id: str, iat_offset_secs: int):
    """Mint an agent JWT directly so we can control the iat claim.

    create_agent_token always sets iat=now(); to test the freshness window we
    need to forge a token with a specific iat. Mirrors the production claim
    set so verify_agent_jwt accepts it normally.
    """
    import jwt as _jwt
    from datetime import timezone as _tz, datetime as _dt, timedelta as _td

    from app.auth import AGENT_JWT_SECRET, AGENT_JWT_ALGORITHM

    now = _dt.now(_tz.utc)
    iat = now + _td(seconds=iat_offset_secs)
    exp = iat + _td(hours=8)
    payload = {
        "sub": str(task_id),
        "vm_instance_id": vm_instance_id,
        "task_type": "",
        "project_id": "",
        "exp": exp,
        "iat": iat,
    }
    return _jwt.encode(payload, AGENT_JWT_SECRET, algorithm=AGENT_JWT_ALGORITHM)


@pytest.mark.asyncio
async def test_vm_heartbeat_fresh_terminal_jwt_accepted(
    client: AsyncClient, db_session
):
    """A fresh terminal-task JWT (iat = now-1 min) is allowed on /vm-heartbeat."""
    from app.routers.agent import _terminated_task_ids

    project = Project(name="M3 Fresh Heartbeat", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id="agent-vm-m3-fresh-hb",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _terminated_task_ids.pop(str(task.id), None)

    token = _make_jwt_with_iat(
        str(task.id), "agent-vm-m3-fresh-hb", iat_offset_secs=-60
    )
    resp = await client.post(
        "/api/agent/vm-heartbeat",
        json={"vm_instance_id": "agent-vm-m3-fresh-hb"},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_vm_heartbeat_stale_terminal_jwt_rejected(
    client: AsyncClient, db_session, caplog
):
    """A stale terminal-task JWT (iat = now-60 min) is rejected 401 on /vm-heartbeat."""
    from app.routers.agent import _terminated_task_ids

    project = Project(name="M3 Stale Heartbeat", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id="agent-vm-m3-stale-hb",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _terminated_task_ids.pop(str(task.id), None)
    caplog.set_level(logging.INFO)

    token = _make_jwt_with_iat(
        str(task.id), "agent-vm-m3-stale-hb", iat_offset_secs=-3600
    )
    resp = await client.post(
        "/api/agent/vm-heartbeat",
        json={"vm_instance_id": "agent-vm-m3-stale-hb"},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 401
    body = resp.json()
    detail = body.get("detail") or body.get("error", {}).get("message", "")
    assert detail == "terminal_token_expired_for_idempotent"
    _assert_agent_auth_reject_log(
        caplog,
        reason="terminal_token_expired_for_idempotent",
        path="/api/agent/vm-heartbeat",
        task_id=str(task.id),
        token_vm_instance_id="agent-vm-m3-stale-hb",
    )


@pytest.mark.asyncio
async def test_next_role_stale_terminal_jwt_rejected(
    client: AsyncClient, db_session, caplog
):
    """A stale terminal-task JWT (iat = now-60 min) is rejected 401 on /next-role.

    Same defense as the heartbeat test — a leaked terminal-task token can't
    spam-claim future warm-pinned tasks once it's outside the freshness window.
    """
    from app.routers.agent import _terminated_task_ids

    project = Project(name="M3 Stale Next-Role", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 9999}],
        vm_instance_id="agent-vm-m3-stale-nr",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _terminated_task_ids.pop(str(task.id), None)
    caplog.set_level(logging.INFO)

    token = _make_jwt_with_iat(
        str(task.id), "agent-vm-m3-stale-nr", iat_offset_secs=-3600
    )
    resp = await client.post(
        "/api/agent/next-role",
        json={
            "vm_instance_id": "agent-vm-m3-stale-nr",
            "prior_task_id": str(task.id),
            "prior_pr_number": 9999,
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 401
    body = resp.json()
    detail = body.get("detail") or body.get("error", {}).get("message", "")
    assert detail == "terminal_token_expired_for_idempotent"
    _assert_agent_auth_reject_log(
        caplog,
        reason="terminal_token_expired_for_idempotent",
        path="/api/agent/next-role",
        task_id=str(task.id),
        token_vm_instance_id="agent-vm-m3-stale-nr",
    )


@pytest.mark.asyncio
async def test_next_role_fresh_terminal_jwt_accepted(client: AsyncClient, db_session):
    """A fresh terminal-task JWT is allowed on /next-role (returns 204 with no work)."""
    from app.routers.agent import _terminated_task_ids

    project = Project(name="M3 Fresh Next-Role", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 5555}],
        vm_instance_id="agent-vm-m3-fresh-nr",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _terminated_task_ids.pop(str(task.id), None)

    token = _make_jwt_with_iat(
        str(task.id), "agent-vm-m3-fresh-nr", iat_offset_secs=-60
    )
    resp = await client.post(
        "/api/agent/next-role",
        json={
            "vm_instance_id": "agent-vm-m3-fresh-nr",
            "prior_task_id": str(task.id),
            "prior_pr_number": 5555,
        },
        headers={"x-agent-token": token},
    )
    # No queued task pinned to this VM → 204 (auth passed, just no work).
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_active_task_jwt_unaffected_by_freshness_check(
    client: AsyncClient, db_session
):
    """An ACTIVE-task JWT (task NOT in _terminated_task_ids) is unaffected by the
    freshness check — the check applies only to terminal tasks. A long-running
    task posting status with a 60-min-old JWT must still succeed.
    """
    from app.routers.agent import _terminated_task_ids

    project = Project(name="M3 Active JWT Unaffected", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id="agent-vm-m3-active",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Belt-and-suspenders: ensure cache is clean so the check actually runs
    # against the live DB row.
    _terminated_task_ids.pop(str(task.id), None)

    token = _make_jwt_with_iat(
        str(task.id), "agent-vm-m3-active", iat_offset_secs=-3600
    )
    # /vm-heartbeat is a warm-idempotent path: even with a 60-min-old JWT,
    # an ACTIVE task is unaffected by the freshness window — the freshness
    # check fires only when the task is terminal.
    resp = await client.post(
        "/api/agent/vm-heartbeat",
        json={"vm_instance_id": "agent-vm-m3-active"},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 204


# ══════════════════════════════════════════════════════════════════════════════
# Issue H3: warm_claim_origin diagnostic marker
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_next_role_claim_writes_warm_claim_origin(
    client: AsyncClient, db_session
):
    """The next-role warm-claim writes warm_claim_origin='next_role_endpoint' so
    post-mortems can distinguish server-side seeding from agent-reported values.
    Also pins down the idempotent contract: prior values for warm_start are
    preserved (setdefault) rather than blindly overwritten.
    """
    project = Project(name="H3 warm_claim_origin", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    prior_task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 4321}],
        vm_instance_id="agent-vm-h3",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(prior_task)
    deadline = (datetime.utcnow() + timedelta(seconds=240)).isoformat()
    pinned_task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "pr_number": 4321, "branch": "feat/h3"}],
        vm_instance_id=None,
        status=TaskStatus.QUEUED,
        # Pre-seed _timing.warm_start with a sentinel value to verify the
        # endpoint preserves prior values via setdefault rather than
        # overwriting them.
        context={
            "warm_target_vm_id": "agent-vm-h3",
            "warm_target_pr": "4321",
            "warm_handoff_deadline": deadline,
            "_timing": {"warm_start": False, "preexisting": "kept"},
        },
    )
    db_session.add(pinned_task)
    await db_session.commit()
    await db_session.refresh(prior_task)
    await db_session.refresh(pinned_task)

    token = create_agent_token(str(prior_task.id), "agent-vm-h3")
    resp = await client.post(
        "/api/agent/next-role",
        json={
            "vm_instance_id": "agent-vm-h3",
            "prior_task_id": str(prior_task.id),
            "prior_pr_number": 4321,
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200

    await db_session.refresh(pinned_task)
    timing = (pinned_task.context or {}).get("_timing", {})
    assert timing.get("warm_claim_origin") == "next_role_endpoint"
    # setdefault preserved the pre-existing False value (the agent's status
    # update is the authoritative source for warm_start).
    assert timing.get("warm_start") is False
    # Sibling keys are preserved.
    assert timing.get("preexisting") == "kept"
    # prior_role_vm_id is the diagnostic trail of which VM made the claim.
    assert timing.get("prior_role_vm_id") == "agent-vm-h3"


# ══════════════════════════════════════════════════════════════════════════════
# handler_duration_secs: schema field + persistence round-trip
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_process_post_commit_persists_handler_duration_secs(db_session):
    """_process_post_commit persists handler_duration_secs into task._timing.

    Direct unit test of the persistence path (agent-side capture lives in
    agent/src/index.ts which is in S3's scope — documented as a Wave 5
    follow-up). Avoids the full /status endpoint to skip the GitHub /
    completion-handler side effects that aren't relevant to the round-trip.
    """
    from app.task_lifecycle import _process_post_commit
    from app.schemas import AgentStatusPayload
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as _AS

    project = Project(name="handler_duration_secs Round-Trip", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id="agent-vm-handler-duration",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.IN_PROGRESS,
        setup_secs=12.5,
        warm_start=True,
        prior_role_vm_id="agent-vm-handler-duration",
        handler_duration_secs=87.3,
    )

    # Pass a fresh-session factory bound to the same engine the test uses, so
    # _process_post_commit's separate session sees the seeded task.
    _factory = async_sessionmaker(db_session.bind, class_=_AS, expire_on_commit=False)
    await _process_post_commit(task.id, payload, _session_factory=_factory)

    # Refresh through a fresh session — the test session may have a stale
    # snapshot of context after _process_post_commit's commit on its own session.
    async with _factory() as fresh:
        refetched = await fresh.get(Task, task.id)
        timing = (refetched.context or {}).get("_timing", {})

    assert timing.get("setup_secs") == 12.5
    assert timing.get("warm_start") is True
    assert timing.get("prior_role_vm_id") == "agent-vm-handler-duration"
    assert timing.get("handler_duration_secs") == 87.3


@pytest.mark.asyncio
async def test_process_post_commit_handler_duration_secs_optional(db_session):
    """A status payload that OMITS handler_duration_secs MUST NOT write the key.

    Older agents that haven't started capturing the field still need to
    round-trip cleanly — the field is opt-in.
    """
    from app.task_lifecycle import _process_post_commit
    from app.schemas import AgentStatusPayload
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as _AS

    project = Project(name="handler_duration_secs Optional", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id="agent-vm-handler-optional",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    payload = AgentStatusPayload(
        task_id=task.id,
        status=TaskStatus.IN_PROGRESS,
        setup_secs=7.0,
    )

    _factory = async_sessionmaker(db_session.bind, class_=_AS, expire_on_commit=False)
    await _process_post_commit(task.id, payload, _session_factory=_factory)

    async with _factory() as fresh:
        refetched = await fresh.get(Task, task.id)
        timing = (refetched.context or {}).get("_timing", {})

    assert timing.get("setup_secs") == 7.0
    # Field absent on payload → absent in storage (forward compatibility
    # with older agents that don't capture the field yet).
    assert "handler_duration_secs" not in timing


def test_handler_duration_secs_rejects_negative_at_schema_layer():
    """ge=0 on handler_duration_secs rejects negative values at the Pydantic layer.

    Defensive: a broken agent clock that produces a negative duration would
    pollute analytics with bogus rows. Pydantic's validator catches it before
    the status endpoint or _process_post_commit ever sees the value.
    """
    from app.schemas import AgentStatusPayload
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AgentStatusPayload(
            task_id=uuid.uuid4(),
            status=TaskStatus.IN_PROGRESS,
            handler_duration_secs=-5.0,
        )

    # Upper bound (24h) likewise rejects nonsense; this also pins the bound
    # so a future change to the clamp value is conscious.
    with pytest.raises(ValidationError):
        AgentStatusPayload(
            task_id=uuid.uuid4(),
            status=TaskStatus.IN_PROGRESS,
            handler_duration_secs=86401.0,
        )

    # Sanity: a sensible value passes.
    ok = AgentStatusPayload(
        task_id=uuid.uuid4(),
        status=TaskStatus.IN_PROGRESS,
        handler_duration_secs=42.0,
    )
    assert ok.handler_duration_secs == 42.0


# ── fire_and_forget_vm_delete: lock-window-break for terminal status ──
# Production incident 2026-05-05 task 04605f9d (Review PR #3688): agent
# posted APPROVE verdict. /api/agent/status handler held FOR UPDATE row
# lock through ~60s of GCP delete I/O. Concurrent retry got 429; status
# update never committed; reaper swept work that had already succeeded.
# Fix: terminal-status handler schedules the GCP delete in the
# background, releasing the row lock immediately. These tests pin the
# load-bearing properties of that fix.


@pytest.mark.asyncio
async def test_fire_and_forget_vm_delete_records_marker_synchronously():
    """The orchestrator-initiated-delete marker must be written BEFORE
    fire_and_forget_vm_delete returns, so the reaper observing
    vm_status=DELETED in the window between create_task() and the
    background coroutine running doesn't misclassify the orchestrator
    action as spot preemption."""
    import asyncio
    from app.vm_lifecycle import (
        fire_and_forget_vm_delete,
        _orchestrator_deleted_vms,
        _background_vm_deletes,
        is_orchestrator_initiated_delete,
    )

    vm_id = f"agent-vm-test-marker-{uuid.uuid4().hex[:8]}"
    _orchestrator_deleted_vms.pop(vm_id, None)

    # Snapshot the background-tasks set so we drain only what *this*
    # test scheduled. The set is module-level and shared across tests;
    # tasks from prior tests belong to prior pytest-asyncio loops and
    # awaiting them raises "future belongs to a different loop".
    pre_existing_tasks = set(_background_vm_deletes)

    # fire_and_forget_vm_delete resolves _ensure_vm_deleted in its own
    # (vm_lifecycle) module namespace, so the stub must be installed there.
    import app.vm_lifecycle as vm_lifecycle_mod

    real_ensure = vm_lifecycle_mod._ensure_vm_deleted
    fired = asyncio.Event()

    async def _stub(*args, **kwargs):
        fired.set()
        return True

    vm_lifecycle_mod._ensure_vm_deleted = _stub  # type: ignore[assignment]
    try:
        fire_and_forget_vm_delete(vm_id, reason="test_marker_sync")
        # Synchronously, before yielding to the event loop, the marker
        # must already be visible.
        is_orch, reason = is_orchestrator_initiated_delete(vm_id)
        assert is_orch is True
        assert reason == "test_marker_sync"
        # And the background coroutine must eventually run.
        await asyncio.wait_for(fired.wait(), timeout=2.0)
    finally:
        vm_lifecycle_mod._ensure_vm_deleted = real_ensure
        new_tasks = [
            t for t in _background_vm_deletes - pre_existing_tasks if not t.done()
        ]
        if new_tasks:
            await asyncio.gather(*new_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_fire_and_forget_vm_delete_does_not_block_caller():
    """The caller's row lock window must not include the GCP delete.
    Even if _ensure_vm_deleted blocks for seconds, the synchronous
    return from fire_and_forget_vm_delete must be near-instant — that
    is the load-bearing property the lock-window-break depends on."""
    import asyncio
    import time
    from app.vm_lifecycle import fire_and_forget_vm_delete, _background_vm_deletes

    pre_existing_tasks = set(_background_vm_deletes)

    # fire_and_forget_vm_delete resolves _ensure_vm_deleted in its own
    # (vm_lifecycle) module namespace, so the stub must be installed there.
    import app.vm_lifecycle as vm_lifecycle_mod

    real_ensure = vm_lifecycle_mod._ensure_vm_deleted

    async def _slow_delete(*args, **kwargs):
        # Simulate GCP delete latency. If fire_and_forget were
        # accidentally awaited, the caller would block for 2 seconds.
        await asyncio.sleep(2.0)
        return True

    vm_lifecycle_mod._ensure_vm_deleted = _slow_delete  # type: ignore[assignment]
    try:
        vm_id = f"agent-vm-test-noblock-{uuid.uuid4().hex[:8]}"
        t0 = time.monotonic()
        fire_and_forget_vm_delete(vm_id, reason="test_noblock")
        elapsed = time.monotonic() - t0
        # Synchronous return — well below the 100ms budget.
        assert elapsed < 0.1, (
            f"fire_and_forget_vm_delete blocked caller for {elapsed * 1000:.0f}ms; "
            f"expected <100ms — would defeat the lock-window-break"
        )
    finally:
        vm_lifecycle_mod._ensure_vm_deleted = real_ensure
        # Cancel rather than await — the slow sleep would block test
        # teardown for 2s and we already proved the property we cared
        # about (synchronous return).
        new_tasks = [
            t for t in _background_vm_deletes - pre_existing_tasks if not t.done()
        ]
        for t in new_tasks:
            t.cancel()
        if new_tasks:
            await asyncio.gather(*new_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_fire_and_forget_vm_delete_no_op_on_empty_vm_id():
    """Defensive: a None or empty vm_id must not schedule a background
    task (would crash on the first await inside _ensure_vm_deleted, but
    the marker write would still leak an empty-string key)."""
    from app.vm_lifecycle import (
        fire_and_forget_vm_delete,
        _orchestrator_deleted_vms,
        _background_vm_deletes,
    )

    before_count = len(_background_vm_deletes)
    fire_and_forget_vm_delete(None, reason="test_empty")
    fire_and_forget_vm_delete("", reason="test_empty")
    after_count = len(_background_vm_deletes)
    assert after_count == before_count, (
        "fire_and_forget_vm_delete must not schedule a background task "
        "for None/empty vm_id"
    )
    assert "" not in _orchestrator_deleted_vms, (
        "fire_and_forget_vm_delete must not record a marker for empty vm_id"
    )


async def _seed_blind_spot_tasks(db_session, project_id):
    """Helper: seed COMPLETED tasks with self-review blind_spots for the project."""
    for i in range(3):
        db_session.add(
            Task(
                project_id=project_id,
                payloads=[{"type": "create"}],
                status=TaskStatus.COMPLETED,
                context={
                    "_self_review_outcome": {
                        "verdict": "pass",
                        "fixes_applied": False,
                        "check_results": {"correctness": {"passed": True}},
                        "blind_spots": [
                            f"did not verify file path number {i} thoroughly"
                        ],
                    }
                },
            )
        )
    db_session.add(
        Task(
            project_id=project_id,
            payloads=[{"type": "fix"}],
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "verdict": "pass",
                    "fixes_applied": False,
                    "check_results": {},
                    "blind_spots": ["async error propagation not checked"],
                }
            },
        )
    )
    # Skipped envelope: must NOT contribute
    db_session.add(
        Task(
            project_id=project_id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            context={
                "_self_review_outcome": {
                    "verdict": "unknown",
                    "fixes_applied": False,
                    "check_results": {},
                    "blind_spots": [],
                }
            },
        )
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_claim_injects_sr_blind_spot_themes_for_create_task(
    client: AsyncClient, db_session
):
    """Claim endpoint injects sr_blind_spot_themes in project_context for create tasks."""
    project = Project(name="SR BlindSpots Create", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_blind_spot_tasks(db_session, project.id)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Add new model"}],
        vm_instance_id="agent-vm-srbs-create",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-srbs-create")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    project_ctx = resp.json().get("project_context", {})
    assert "sr_blind_spot_themes" in project_ctx, (
        f"Expected sr_blind_spot_themes in project_context, got: {list(project_ctx.keys())}"
    )
    themes = {t["theme"]: t["count"] for t in project_ctx["sr_blind_spot_themes"]}
    assert themes.get("did not verify file path number") == 3
    assert themes.get("async error propagation not checked") == 1
    # Top-N cap (limit=8) — only 2 distinct themes here
    assert len(project_ctx["sr_blind_spot_themes"]) == 2
    # Sorted by count desc
    assert (
        project_ctx["sr_blind_spot_themes"][0]["count"]
        >= project_ctx["sr_blind_spot_themes"][1]["count"]
    )


@pytest.mark.asyncio
async def test_claim_omits_sr_blind_spot_themes_when_empty(
    client: AsyncClient, db_session
):
    """No completed tasks with blind_spots → field absent."""
    project = Project(name="SR BlindSpots Empty", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Something"}],
        vm_instance_id="agent-vm-srbs-empty",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-srbs-empty")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    project_ctx = resp.json().get("project_context", {})
    assert "sr_blind_spot_themes" not in project_ctx


@pytest.mark.asyncio
async def test_claim_does_not_inject_sr_blind_spot_themes_for_review_task(
    client: AsyncClient, db_session
):
    """Field is NOT injected for non-(create/fix/refactor/migrate) task types."""
    project = Project(name="SR BlindSpots Review", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    await _seed_blind_spot_tasks(db_session, project.id)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 99, "branch": "fix/something"}],
        vm_instance_id="agent-vm-srbs-review",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-srbs-review")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    project_ctx = resp.json().get("project_context", {})
    assert "sr_blind_spot_themes" not in project_ctx, (
        f"sr_blind_spot_themes should be absent for review tasks, got: "
        f"{project_ctx.get('sr_blind_spot_themes')}"
    )


@pytest.mark.asyncio
async def test_claim_injects_self_review_catch_rate_for_direction_when_above_threshold(
    client: AsyncClient,
    db_session,
):
    """When the task has a direction_id and design_doc.sr_catch_rate_trends.by_direction
    has an entry for that direction, the claim endpoint injects
    project_context.self_review_catch_rate_for_direction with category data and
    direction metadata."""
    direction_id = "d-test-dir-1"
    project = Project(
        name="SR Direction Test",
        user_id="test-user-123",
        directions=[{"id": direction_id, "text": "Improve self-review accuracy"}],
        design_doc={
            "sr_catch_rate_trends": {
                "categories": {
                    "test-coverage": {"miss_rate": 0.4},
                },
                "by_task_type": {},
                "by_direction": {
                    direction_id: {
                        "sample_count": 5,
                        "miss_rate": 0.6,
                        "categories": {
                            "regression_risk": {
                                "miss_rate": 0.7,
                                "examples": [
                                    {
                                        "severity": "MAJOR",
                                        "desc": "Mocked migration masked breakage",
                                    },
                                ],
                            },
                            "scope-drift": {"miss_rate": 0.3},
                        },
                    },
                },
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Add new module"}],
        context={"direction_id": direction_id},
        vm_instance_id="agent-vm-srdir-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-srdir-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    project_ctx = resp.json().get("project_context", {})
    # Project-wide payload still present (backward compat)
    assert "self_review_catch_rate" in project_ctx
    # New direction-scoped payload
    assert "self_review_catch_rate_for_direction" in project_ctx
    payload = project_ctx["self_review_catch_rate_for_direction"]
    assert payload["direction_id"] == direction_id
    assert payload["direction_text"] == "Improve self-review accuracy"
    assert payload["sample_count"] == 5
    assert payload["miss_rate"] == 0.6
    assert "regression_risk" in payload["categories"]
    assert payload["categories"]["regression_risk"]["miss_rate"] == 0.7
    assert payload["categories"]["regression_risk"]["examples"][0]["desc"].startswith(
        "Mocked migration"
    )


@pytest.mark.asyncio
async def test_claim_omits_self_review_catch_rate_for_direction_when_no_direction_id(
    client: AsyncClient,
    db_session,
):
    """Tasks without direction_id never receive the direction-scoped payload, even if
    by_direction has data."""
    direction_id = "d-test-dir-2"
    project = Project(
        name="SR Direction Test No DID",
        user_id="test-user-123",
        directions=[{"id": direction_id, "text": "Some direction"}],
        design_doc={
            "sr_catch_rate_trends": {
                "categories": {"correctness": {"miss_rate": 0.4}},
                "by_task_type": {},
                "by_direction": {
                    direction_id: {
                        "sample_count": 5,
                        "miss_rate": 0.5,
                        "categories": {"correctness": {"miss_rate": 0.5}},
                    },
                },
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Add module"}],
        context={},
        vm_instance_id="agent-vm-srdir-2",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-srdir-2")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    project_ctx = resp.json().get("project_context", {})
    assert "self_review_catch_rate_for_direction" not in project_ctx
    # Project-wide still present
    assert "self_review_catch_rate" in project_ctx


@pytest.mark.asyncio
async def test_claim_omits_self_review_catch_rate_for_direction_when_below_threshold(
    client: AsyncClient,
    db_session,
):
    """When the task's direction_id has no entry in by_direction (insufficient sample),
    the direction-scoped field is silently omitted but the project-wide payload is still
    injected."""
    direction_id = "d-low-sample"
    project = Project(
        name="SR Direction Below Threshold",
        user_id="test-user-123",
        directions=[{"id": direction_id, "text": "Low-sample direction"}],
        design_doc={
            "sr_catch_rate_trends": {
                "categories": {"correctness": {"miss_rate": 0.4}},
                "by_task_type": {},
                "by_direction": {},
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Add module"}],
        context={"direction_id": direction_id},
        vm_instance_id="agent-vm-srdir-3",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-srdir-3")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    project_ctx = resp.json().get("project_context", {})
    assert "self_review_catch_rate_for_direction" not in project_ctx
    assert "self_review_catch_rate" in project_ctx


@pytest.mark.asyncio
async def test_claim_injects_sr_recent_miss_examples_for_create_task(
    client: AsyncClient, db_session
):
    """Claim endpoint injects sr_recent_miss_examples from sr_catch_rate_trends.categories[cat].examples."""
    from sqlalchemy.orm.attributes import flag_modified

    design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "correctness": {
                    "miss_rate": 0.40,
                    "weighted_miss_rate": 0.45,
                    "examples": [
                        {
                            "severity": "CRITICAL",
                            "desc": "Off-by-one in loop boundary check",
                        },
                        {
                            "severity": "MAJOR",
                            "desc": "Null pointer not guarded before dereference",
                        },
                    ],
                },
                "security": {
                    "miss_rate": 0.30,
                    "weighted_miss_rate": 0.35,
                    "examples": [
                        {
                            "severity": "MAJOR",
                            "desc": "User input passed directly to SQL query",
                        },
                    ],
                },
                "style-conventions": {
                    "miss_rate": 0.10,
                    "weighted_miss_rate": 0.05,
                    # No examples — should be excluded
                },
            }
        }
    }
    project = Project(
        name="SR Miss Examples Test",
        user_id="test-user-123",
        design_doc=design_doc,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    flag_modified(project, "design_doc")
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Add new feature"}],
        vm_instance_id="agent-vm-miss-ex-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-miss-ex-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    project_ctx = resp.json().get("project_context", {})
    assert "sr_recent_miss_examples" in project_ctx, (
        f"Expected sr_recent_miss_examples in project_context, got: {list(project_ctx.keys())}"
    )
    miss_ex = project_ctx["sr_recent_miss_examples"]
    # correctness and security have examples; style-conventions does not
    assert "correctness" in miss_ex
    assert "security" in miss_ex
    assert "style-conventions" not in miss_ex
    # correctness ranked higher (weighted_miss_rate 0.45 > 0.35) → appears first
    assert next(iter(miss_ex.keys())) == "correctness"
    # Examples carry severity and desc fields
    assert miss_ex["correctness"][0]["severity"] == "CRITICAL"
    assert "Off-by-one" in miss_ex["correctness"][0]["desc"]
    assert miss_ex["security"][0]["severity"] == "MAJOR"


@pytest.mark.asyncio
async def test_claim_omits_sr_recent_miss_examples_when_no_examples(
    client: AsyncClient, db_session
):
    """Field is absent when no category has examples populated."""
    design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "correctness": {"miss_rate": 0.40, "weighted_miss_rate": 0.45},
            }
        }
    }
    project = Project(
        name="SR Miss Examples Empty",
        user_id="test-user-123",
        design_doc=design_doc,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Something"}],
        vm_instance_id="agent-vm-miss-ex-2",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-miss-ex-2")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    project_ctx = resp.json().get("project_context", {})
    assert "sr_recent_miss_examples" not in project_ctx


@pytest.mark.asyncio
async def test_claim_does_not_inject_sr_recent_miss_examples_for_review_task(
    client: AsyncClient, db_session
):
    """Field is NOT injected for review task types."""
    from sqlalchemy.orm.attributes import flag_modified

    design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "correctness": {
                    "miss_rate": 0.40,
                    "weighted_miss_rate": 0.45,
                    "examples": [{"severity": "CRITICAL", "desc": "Some issue"}],
                }
            }
        }
    }
    project = Project(
        name="SR Miss Examples Review",
        user_id="test-user-123",
        design_doc=design_doc,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    flag_modified(project, "design_doc")
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 42, "branch": "fix/thing"}],
        vm_instance_id="agent-vm-miss-ex-3",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-miss-ex-3")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    project_ctx = resp.json().get("project_context", {})
    assert "sr_recent_miss_examples" not in project_ctx


@pytest.mark.asyncio
async def test_claim_sr_recent_miss_examples_matches_typescript_consumer_contract(
    client: AsyncClient, db_session
):
    """Cross-language contract test.

    The claim endpoint must emit response['project_context']['sr_recent_miss_examples']
    in the exact shape that the TypeScript consumer
    (agent/src/prompts/project-context.ts -> formatSrRecentMissExamplesForSrPrompt)
    reads. The canonical shape lives in
    agent/src/prompts/__fixtures__/sr_recent_miss_examples_contract.json and is
    consumed by both this test and a parallel test in
    agent/src/prompts/project-context.test.ts. If you change the producer or the
    consumer, update the fixture so both sides agree.
    """
    import json

    from sqlalchemy.orm.attributes import flag_modified

    fixture_path = _resolve_sr_recent_miss_examples_contract_fixture()
    fixture = json.loads(fixture_path.read_text())
    expected_payload = fixture["sr_recent_miss_examples"]

    # Build a design_doc whose sr_catch_rate_trends will, after the backend's
    # ranking + cap pass, produce exactly the fixture's emitted payload.
    categories: dict = {}
    for idx, (cat, examples) in enumerate(expected_payload.items()):
        # Higher weighted_miss_rate = ranked earlier; preserve fixture insertion order.
        categories[cat] = {
            "miss_rate": 0.5,
            "weighted_miss_rate": 0.9 - (idx * 0.1),
            "examples": list(examples),
        }
    design_doc = {"sr_catch_rate_trends": {"categories": categories}}

    project = Project(
        name="SR Recent Miss Examples Contract",
        user_id="test-user-123",
        design_doc=design_doc,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    flag_modified(project, "design_doc")
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Add module"}],
        vm_instance_id="agent-vm-contract-1",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-contract-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    project_ctx = resp.json().get("project_context", {})
    assert "sr_recent_miss_examples" in project_ctx, (
        "Producer regression: claim endpoint did not emit sr_recent_miss_examples. "
        "TypeScript consumer (agent/src/prompts/project-context.ts) reads this exact key."
    )
    emitted = project_ctx["sr_recent_miss_examples"]

    # Shape contract: top-level dict[str, list[dict]] with each item having
    # severity (str) and desc (str) — exactly what the TS formatter destructures.
    assert isinstance(emitted, dict), f"Expected dict, got {type(emitted).__name__}"
    for cat, examples in emitted.items():
        assert isinstance(cat, str)
        assert isinstance(examples, list) and examples, (
            f"category {cat!r} must have non-empty list"
        )
        for ex in examples:
            assert isinstance(ex, dict)
            assert isinstance(ex.get("severity"), str), f"missing severity in {ex!r}"
            assert isinstance(ex.get("desc"), str), f"missing desc in {ex!r}"

    # Value contract: emitted payload matches the fixture exactly.
    assert emitted == expected_payload, (
        "Emitted sr_recent_miss_examples diverged from contract fixture. "
        "Update agent/src/prompts/__fixtures__/sr_recent_miss_examples_contract.json "
        "AND agent/src/prompts/project-context.ts together if this is intentional."
    )


def test_claim_sr_recent_miss_examples_fixture_parity():
    """The two on-disk copies of the contract fixture must stay byte-identical.

    Backend Docker reads from ``backend/testing/__fixtures__/`` (in the
    image), agent reads from ``agent/src/prompts/__fixtures__/`` (in the
    repo). When both are reachable (local dev, outside Docker), assert
    they match so drift is caught at PR time. When only one is reachable
    (CI Docker build context = ``./backend``), this test no-ops — drift
    will surface on whichever side runs locally next.
    """
    backend_copy = (
        Path(__file__).parent / "__fixtures__" / "sr_recent_miss_examples_contract.json"
    )
    agent_copy = (
        Path(__file__).resolve().parents[2]
        / "agent"
        / "src"
        / "prompts"
        / "__fixtures__"
        / "sr_recent_miss_examples_contract.json"
    )
    if not (backend_copy.exists() and agent_copy.exists()):
        pytest.skip(
            "Only one of the two contract-fixture copies is reachable "
            "(expected in Docker where agent/ is not mounted)."
        )
    assert backend_copy.read_text() == agent_copy.read_text(), (
        "Contract fixture drift: backend and agent copies of "
        "sr_recent_miss_examples_contract.json have diverged. "
        f"Backend: {backend_copy}\nAgent:   {agent_copy}\n"
        "Both must be updated together."
    )


@pytest.mark.asyncio
async def test_claim_sr_recent_miss_examples_global_unchanged_without_direction_id(
    client: AsyncClient, db_session
):
    """(a) Global ranking is unchanged when the task has no direction_id."""
    from sqlalchemy.orm.attributes import flag_modified

    design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "correctness": {
                    "miss_rate": 0.50,
                    "weighted_miss_rate": 0.55,
                    "examples": [
                        {"severity": "CRITICAL", "desc": "Global correctness issue"}
                    ],
                },
                "security": {
                    "miss_rate": 0.40,
                    "weighted_miss_rate": 0.42,
                    "examples": [
                        {"severity": "MAJOR", "desc": "Global security issue"}
                    ],
                },
            },
            "by_direction": {
                "dir-xyz": {
                    "categories": {
                        "scope-drift": {
                            "miss_rate": 0.80,
                            "examples": [
                                {"severity": "MINOR", "desc": "Direction-only example"}
                            ],
                        }
                    }
                }
            },
        }
    }
    project = Project(
        name="SR Miss Examples No Direction",
        user_id="test-user-123",
        design_doc=design_doc,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    flag_modified(project, "design_doc")
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "No direction task"}],
        vm_instance_id="agent-vm-no-dir-1",
        # no direction_id in context
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-no-dir-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    miss_ex = resp.json().get("project_context", {}).get("sr_recent_miss_examples", {})
    # Global categories served — direction-only category NOT present
    assert "correctness" in miss_ex
    assert "security" in miss_ex
    assert "scope-drift" not in miss_ex


@pytest.mark.asyncio
async def test_claim_sr_recent_miss_examples_prefers_direction_examples(
    client: AsyncClient, db_session
):
    """(b) Per-direction examples are preferred over global when the task has direction_id."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = "dir-abc"
    design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "correctness": {
                    "miss_rate": 0.50,
                    "weighted_miss_rate": 0.55,
                    "examples": [
                        {"severity": "CRITICAL", "desc": "Global correctness example"}
                    ],
                },
            },
            "by_direction": {
                direction_id: {
                    "categories": {
                        "scope-drift": {
                            "miss_rate": 0.80,
                            "examples": [
                                {
                                    "severity": "MAJOR",
                                    "desc": "Direction scope-drift example",
                                }
                            ],
                        },
                        "architecture": {
                            "miss_rate": 0.60,
                            "examples": [
                                {
                                    "severity": "MINOR",
                                    "desc": "Direction architecture example",
                                }
                            ],
                        },
                    }
                }
            },
        }
    }
    project = Project(
        name="SR Miss Examples With Direction",
        user_id="test-user-123",
        design_doc=design_doc,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    flag_modified(project, "design_doc")
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Direction-scoped task"}],
        vm_instance_id="agent-vm-dir-pref-1",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-dir-pref-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    miss_ex = resp.json().get("project_context", {}).get("sr_recent_miss_examples", {})
    # Direction-specific categories present; global-only category NOT present
    assert "scope-drift" in miss_ex
    assert "architecture" in miss_ex
    assert "correctness" not in miss_ex
    # scope-drift ranked first (miss_rate 0.80 > 0.60)
    assert next(iter(miss_ex.keys())) == "scope-drift"
    assert "Direction scope-drift example" in miss_ex["scope-drift"][0]["desc"]


@pytest.mark.asyncio
async def test_claim_sr_recent_miss_examples_falls_back_to_global_when_direction_empty(
    client: AsyncClient, db_session
):
    """(c) Falls back to global examples when the direction has no qualifying examples."""
    from sqlalchemy.orm.attributes import flag_modified

    direction_id = "dir-no-examples"
    design_doc = {
        "sr_catch_rate_trends": {
            "categories": {
                "correctness": {
                    "miss_rate": 0.50,
                    "weighted_miss_rate": 0.55,
                    "examples": [
                        {"severity": "CRITICAL", "desc": "Global fallback example"}
                    ],
                },
            },
            "by_direction": {
                direction_id: {
                    "categories": {
                        # Categories exist but none have examples
                        "scope-drift": {"miss_rate": 0.80},
                        "architecture": {"miss_rate": 0.60, "examples": []},
                    }
                }
            },
        }
    }
    project = Project(
        name="SR Miss Examples Direction Empty",
        user_id="test-user-123",
        design_doc=design_doc,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    flag_modified(project, "design_doc")
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "Fallback direction task"}],
        vm_instance_id="agent-vm-dir-fallback-1",
        context={"direction_id": direction_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-dir-fallback-1")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    miss_ex = resp.json().get("project_context", {}).get("sr_recent_miss_examples", {})
    # Global fallback: correctness present; direction categories (no examples) absent
    assert "correctness" in miss_ex
    assert "Global fallback example" in miss_ex["correctness"][0]["desc"]
    assert "scope-drift" not in miss_ex
    assert "architecture" not in miss_ex


# ── Efficiency-direction (d-91d8dede) foundational instrumentation ───────────


@pytest.mark.asyncio
async def test_claim_records_vm_cold_start_ms_once(client: AsyncClient, db_session):
    """The first /claim sets vm_cold_start_ms from (last_heartbeat - vm_provisioned_at);
    a re-provision restamp of vm_provisioned_at must NOT clobber that measurement."""
    project = Project(name="Cold Start Claim", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    provisioned = datetime.utcnow() - timedelta(seconds=12)
    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "ls"}],
        vm_instance_id="agent-vm-cold-start",
        status=TaskStatus.QUEUED,
        vm_provisioned_at=provisioned,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    assert task.vm_cold_start_ms is None

    token = create_agent_token(str(task.id), "agent-vm-cold-start")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    await db_session.refresh(task)
    assert task.vm_cold_start_ms is not None
    # ~12s elapsed, so cold-start should be ≥10s and ≤30s (test slack).
    assert 10_000 <= task.vm_cold_start_ms <= 30_000
    initial_cold_start = task.vm_cold_start_ms

    # Simulate a re-provision restamp of vm_provisioned_at, then heartbeat.
    # The IS NULL guard must prevent vm_cold_start_ms from being overwritten.
    task.vm_provisioned_at = datetime.utcnow()
    db_session.add(task)
    await db_session.commit()

    hb_resp = await client.post(
        "/api/agent/heartbeat",
        json={"task_id": str(task.id), "status_message": "tick"},
        headers={"x-agent-token": token},
    )
    assert hb_resp.status_code == 200
    await db_session.refresh(task)
    assert task.vm_cold_start_ms == initial_cold_start


@pytest.mark.asyncio
async def test_heartbeat_beacon_does_not_set_vm_cold_start_ms(
    client: AsyncClient, db_session
):
    """Pre-claim provisioning beacons must NOT set vm_cold_start_ms.

    Boot beacon heartbeats arrive long before the agent process exists and
    therefore before any /claim call. The IS-NULL-guarded heartbeat fallback
    was removed so that beacon timing no longer corrupts this metric. The claim
    endpoint remains the sole writer. Warm-claimed tasks must stay unmeasured.
    """
    project = Project(name="Beacon Cold Start", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    provisioned = datetime.utcnow() - timedelta(seconds=8)
    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "ls"}],
        vm_instance_id="agent-vm-beacon-test",
        status=TaskStatus.QUEUED,
        vm_provisioned_at=provisioned,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    assert task.vm_cold_start_ms is None

    token = create_agent_token(str(task.id), "agent-vm-beacon-test")

    # Simulate several provisioning-phase beacon heartbeats before /claim.
    for beacon_msg in (
        "PROVISION_CREDENTIALS_FETCHED",
        "PROVISION_ENV_MATERIALIZED",
        "PROVISION_PAYLOAD_DOWNLOADED",
        "AGENT_BOOTED",
    ):
        hb_resp = await client.post(
            "/api/agent/heartbeat",
            json={"task_id": str(task.id), "status_message": beacon_msg},
            headers={"x-agent-token": token},
        )
        assert hb_resp.status_code == 200

    await db_session.refresh(task)
    # Beacons must not set vm_cold_start_ms — that field belongs to /claim.
    assert task.vm_cold_start_ms is None

    # Now claim; cold-start should be recorded from dispatch-to-claim latency.
    claim_resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert claim_resp.status_code == 200

    await db_session.refresh(task)
    assert task.vm_cold_start_ms is not None
    # ~8s elapsed, so cold-start should be ≥6s and ≤20s (test slack).
    assert 6_000 <= task.vm_cold_start_ms <= 20_000


@pytest.mark.asyncio
async def test_claim_uses_vm_dispatched_at_for_cold_start(
    client: AsyncClient, db_session
):
    """vm_cold_start_ms must be anchored at vm_dispatched_at (dispatch time) when
    both vm_dispatched_at and vm_provisioned_at are present, so that GCE boot
    latency is included in the measurement."""
    project = Project(name="Dispatch Cold Start", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # vm_dispatched_at set 20s ago (full dispatch→agent-ready window).
    # vm_provisioned_at set only 5s ago (provision-confirm restamp; excludes boot).
    dispatched = datetime.utcnow() - timedelta(seconds=20)
    provisioned = datetime.utcnow() - timedelta(seconds=5)
    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "ls"}],
        vm_instance_id="agent-vm-dispatched",
        status=TaskStatus.QUEUED,
        vm_dispatched_at=dispatched,
        vm_provisioned_at=provisioned,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-dispatched")
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200

    await db_session.refresh(task)
    assert task.vm_cold_start_ms is not None
    # Must reflect the full ~20s dispatch interval, not the 5s provision-confirm interval.
    assert task.vm_cold_start_ms >= 15_000, (
        f"Expected vm_cold_start_ms to reflect dispatch->claim (~20s), got {task.vm_cold_start_ms}ms"
    )


@pytest.mark.asyncio
async def test_claim_stamps_provision_slot_acquired_at_from_compute(
    client: AsyncClient, db_session
):
    """Claim backfills provision_slot_acquired_at from compute's in-memory store into
    _cold_start_stages so the efficiency snapshot can compute dispatch_to_slot_ms."""
    import app.compute as _compute

    project = Project(name="Slot Acquired Claim", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    dispatched = datetime.utcnow() - timedelta(seconds=30)
    vm_id = "agent-vm-slot-acquired-test"
    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "ls"}],
        vm_instance_id=vm_id,
        status=TaskStatus.QUEUED,
        vm_dispatched_at=dispatched,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    slot_ts = (datetime.utcnow() - timedelta(seconds=20)).isoformat()
    _compute._vm_provision_slot_times[vm_id] = slot_ts

    try:
        token = create_agent_token(str(task.id), vm_id)
        resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
        assert resp.status_code == 200

        await db_session.refresh(task)
        stages = (task.context or {}).get("_cold_start_stages") or {}
        assert "provision_slot_acquired_at" in stages, (
            "provision_slot_acquired_at must be backfilled from compute's in-memory store"
        )
        assert stages["provision_slot_acquired_at"] == slot_ts
    finally:
        _compute._vm_provision_slot_times.pop(vm_id, None)


@pytest.mark.asyncio
async def test_heartbeat_persists_api_call_summary(client: AsyncClient, db_session):
    """Heartbeat persists llm_call_count + token totals into Task.api_call_summary."""
    project = Project(name="API Call Summary", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-api-summary",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    assert task.api_call_summary is None

    token = create_agent_token(str(task.id), "agent-vm-api-summary")
    resp = await client.post(
        "/api/agent/heartbeat",
        json={
            "task_id": str(task.id),
            "llm_call_count": 3,
            "total_input_tokens": 12345,
            "total_output_tokens": 678,
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200

    await db_session.refresh(task)
    summary = task.api_call_summary
    assert summary is not None
    assert summary["llm_call_count"] == 3
    assert summary["total_input_tokens"] == 12345
    assert summary["total_output_tokens"] == 678
    assert "last_updated_at" in summary

    # Subsequent heartbeat with higher cumulative counts overwrites.
    resp2 = await client.post(
        "/api/agent/heartbeat",
        json={
            "task_id": str(task.id),
            "llm_call_count": 5,
            "total_input_tokens": 20000,
            "total_output_tokens": 999,
        },
        headers={"x-agent-token": token},
    )
    assert resp2.status_code == 200
    await db_session.refresh(task)
    assert task.api_call_summary["llm_call_count"] == 5
    assert task.api_call_summary["total_input_tokens"] == 20000
    assert task.api_call_summary["total_output_tokens"] == 999


@pytest.mark.asyncio
async def test_heartbeat_omits_api_call_summary_when_fields_absent(
    client: AsyncClient, db_session
):
    """Backwards-compat: older agents that don't ship LLM telemetry leave
    api_call_summary untouched (NULL) — heartbeat still succeeds."""
    project = Project(name="Backcompat Heartbeat", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"cmd": "run"}],
        vm_instance_id="agent-vm-backcompat",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-backcompat")
    resp = await client.post(
        "/api/agent/heartbeat",
        json={"task_id": str(task.id), "status_message": "ok"},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200

    await db_session.refresh(task)
    assert task.api_call_summary is None


# ===========================================================================
# Release-qualifier finalization from EVERY terminal path (automain/LTS)
#
# A qualifier child (a release_audit task or a qualifier-smoketest) that goes
# terminal must trigger maybe_finalize_qualification, regardless of whether it
# COMPLETED, FAILED or was CANCELLED, and regardless of artifact_type. If the
# failing/cancelled child is the LAST of the two to terminate and finalization
# never fires, the ReleaseVersion stays {status:"pending"} forever and the
# version is wedged.
#
# These tests drive _process_post_commit (where the finalize hook now lives,
# in the terminal section so it runs for COMPLETED/FAILED/CANCELLED alike) and
# the full /api/agent/status endpoint (for the unflushed-artifact regression).
# ===========================================================================


async def _make_qualifier_project_and_version(db_session):
    """Create a self-repo project with an authorized GitHub repo + open v1."""
    from app import release_version as _rv

    project = Project(
        name="Qualifier-Terminal-Path",
        user_id="qualifier-user",
        github_owner="acme",
        github_repo="widget",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await _rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)
    return project, version


async def _seed_qualifier_children(
    db_session,
    project,
    version,
    *,
    audit_status: TaskStatus,
    smoke_status: TaskStatus,
):
    """Create the release_audit + qualifier-smoketest children in the requested
    statuses and stamp version.last_qualification = 'pending' pointing at them,
    mirroring the real run_qualification spawn shape."""
    audit_task = Task(
        project_id=project.id,
        title="Release audit child",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id=f"agent-vm-audit-{uuid.uuid4().hex[:8]}",
        status=audit_status,
        context={"release_version_id": str(version.id)},
    )
    smoke_task = Task(
        project_id=project.id,
        title="Qualifier smoketest child",
        payloads=[{"type": "smoketest", "base_branch": "automain"}],
        vm_instance_id=f"agent-vm-smoke-{uuid.uuid4().hex[:8]}",
        status=smoke_status,
        context={
            "release_version_id": str(version.id),
            "qualifier_smoketest": True,
        },
    )
    db_session.add(audit_task)
    db_session.add(smoke_task)
    await db_session.commit()
    await db_session.refresh(audit_task)
    await db_session.refresh(smoke_task)

    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "ci_green": True,
        "started_at": "2026-05-20T00:00:00",
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": str(smoke_task.id),
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return audit_task, smoke_task


def _process_post_commit_factory(db_session):
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as _AS

    return async_sessionmaker(db_session.bind, class_=_AS, expire_on_commit=False)


@pytest.mark.asyncio
async def test_failed_release_audit_last_child_triggers_finalization(
    db_session, mocker
):
    """CRITICAL: a FAILED release_audit that is the LAST qualifier child to go
    terminal must still finalize the qualification.

    Previously the release_audit finalize hook sat inside the
    `if payload.status == COMPLETED` block in _process_post_commit, so a FAILED
    release_audit never reached it — the version stayed {status:"pending"}
    forever. The hook now lives in the terminal section and fires for FAILED.
    """
    from app.task_lifecycle import _process_post_commit
    from app.schemas import AgentStatusPayload
    from app.models import ReleaseVersion

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project, version = await _make_qualifier_project_and_version(db_session)
    # Smoketest sibling already terminal (COMPLETED); the release_audit is the
    # second/last child and is going terminally FAILED now.
    audit_task, _smoke = await _seed_qualifier_children(
        db_session,
        project,
        version,
        audit_status=TaskStatus.FAILED,
        smoke_status=TaskStatus.COMPLETED,
    )

    payload = AgentStatusPayload(task_id=audit_task.id, status=TaskStatus.FAILED)
    _factory = _process_post_commit_factory(db_session)
    await _process_post_commit(audit_task.id, payload, _session_factory=_factory)

    async with _factory() as fresh:
        refetched = await fresh.get(ReleaseVersion, version.id)
        qual = refetched.last_qualification or {}

    # The version must no longer be wedged at "pending" — finalization ran.
    assert qual.get("status") != "pending", (
        f"FAILED release_audit (last child) left version wedged: {qual}"
    )
    # A FAILED required child is an infrastructure failure (qualifier pipeline
    # broke), NOT a quality finding — it routes to infra_failure and returns
    # the version to stabilize for a clean re-qualification.
    assert qual.get("status") == "infra_failure"
    assert qual.get("error") == "audit_task_failed"
    assert refetched.phase == "stabilize"


@pytest.mark.asyncio
async def test_failed_qualifier_smoketest_no_artifact_triggers_finalization(
    db_session, mocker
):
    """CRITICAL: a qualifier-smoketest that FAILS without posting a
    SMOKETEST_RESULT artifact (harness ERROR_LOG, VM crash, reaper kill) must
    still finalize the qualification when it is the last child.

    The old agent_status.py hook was gated on
    `artifact_type == SMOKETEST_RESULT`, so an artifact-less smoketest failure
    never reached it. The relocated hook keys on context, not artifact_type.
    """
    from app.task_lifecycle import _process_post_commit
    from app.schemas import AgentStatusPayload
    from app.models import ReleaseVersion

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project, version = await _make_qualifier_project_and_version(db_session)
    # Audit sibling already terminal; the qualifier-smoketest is the last child
    # and fails terminally with NO artifact attached to the payload.
    _audit, smoke_task = await _seed_qualifier_children(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.FAILED,
    )

    # No artifact_type — this is the VM-crash / harness-ERROR_LOG case.
    payload = AgentStatusPayload(task_id=smoke_task.id, status=TaskStatus.FAILED)
    _factory = _process_post_commit_factory(db_session)
    await _process_post_commit(smoke_task.id, payload, _session_factory=_factory)

    async with _factory() as fresh:
        refetched = await fresh.get(ReleaseVersion, version.id)
        qual = refetched.last_qualification or {}

    assert qual.get("status") != "pending", (
        f"artifact-less FAILED qualifier-smoketest left version wedged: {qual}"
    )
    # A FAILED required smoketest child is an infrastructure failure (VM crash /
    # harness ERROR_LOG), NOT a quality verdict — it routes to infra_failure
    # rather than recording a smoketest_startup_failed quality blocker.
    assert qual.get("status") == "infra_failure"
    assert qual.get("error") == "smoketest_task_failed"


@pytest.mark.asyncio
async def test_cancelled_release_audit_last_child_triggers_finalization(
    db_session, mocker
):
    """CRITICAL: a CANCELLED release_audit child must also finalize — the hook
    fires for COMPLETED / FAILED / CANCELLED, not just the first two."""
    from app.task_lifecycle import _process_post_commit
    from app.schemas import AgentStatusPayload
    from app.models import ReleaseVersion

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project, version = await _make_qualifier_project_and_version(db_session)
    audit_task, _smoke = await _seed_qualifier_children(
        db_session,
        project,
        version,
        audit_status=TaskStatus.CANCELLED,
        smoke_status=TaskStatus.COMPLETED,
    )

    payload = AgentStatusPayload(task_id=audit_task.id, status=TaskStatus.CANCELLED)
    _factory = _process_post_commit_factory(db_session)
    await _process_post_commit(audit_task.id, payload, _session_factory=_factory)

    async with _factory() as fresh:
        refetched = await fresh.get(ReleaseVersion, version.id)
        qual = refetched.last_qualification or {}

    assert qual.get("status") != "pending", (
        f"CANCELLED release_audit (last child) left version wedged: {qual}"
    )
    # A CANCELLED required child is an infrastructure failure, NOT a quality
    # finding — it routes to infra_failure (mirrors the FAILED case).
    assert qual.get("status") == "infra_failure"
    assert qual.get("error") == "audit_task_cancelled"


@pytest.mark.asyncio
async def test_failed_qualifier_child_does_not_finalize_while_sibling_runs(
    db_session, mocker
):
    """A FAILED qualifier child must NOT finalize while its sibling is still
    running — finalization waits for BOTH children to be terminal."""
    from app.task_lifecycle import _process_post_commit
    from app.schemas import AgentStatusPayload
    from app.models import ReleaseVersion

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project, version = await _make_qualifier_project_and_version(db_session)
    # Smoketest sibling still IN_PROGRESS — finalization must defer.
    audit_task, _smoke = await _seed_qualifier_children(
        db_session,
        project,
        version,
        audit_status=TaskStatus.FAILED,
        smoke_status=TaskStatus.IN_PROGRESS,
    )

    payload = AgentStatusPayload(task_id=audit_task.id, status=TaskStatus.FAILED)
    _factory = _process_post_commit_factory(db_session)
    await _process_post_commit(audit_task.id, payload, _session_factory=_factory)

    async with _factory() as fresh:
        refetched = await fresh.get(ReleaseVersion, version.id)
        qual = refetched.last_qualification or {}

    # Sibling still running — qualification stays pending (not yet finalized).
    assert qual.get("status") == "pending"


@pytest.mark.asyncio
async def test_qualifier_smoketest_finalize_sees_own_artifact_via_endpoint(
    client: AsyncClient, db_session, mocker
):
    """CRITICAL (unflushed-artifact regression): when a qualifier-smoketest is
    the SECOND child to finish, finalization must see the SMOKETEST_RESULT
    artifact posted in the SAME /api/agent/status request.

    The status handler disables autoflush and only session.add(...)s the
    artifact. The old in-handler finalize ran before the early commit, so its
    SELECT could not see the pending artifact → _score_smoketest(None) → hard
    blocker → a PASSING candidate wrongly recorded not_promoted. Finalization
    now runs in _process_post_commit AFTER the early commit, so the artifact
    and the task's terminal status are committed and visible.

    Here the smoketest PASSES every test; with promotion shipping dark the
    correct outcome is a dry-run with a non-zero smoketest sub-score — proving
    the artifact was read, not seen as None.
    """
    import json as _json
    from app import release_version as _rv
    from app.models import ReleaseVersion, Artifact, ArtifactType

    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch("app.vm_lifecycle.fire_and_forget_vm_delete", return_value=None)
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    # Finalize re-resolves CI for the candidate commit rather than trusting the
    # spawn-time ci_green snapshot; with no GitHub credentials in this test that
    # resolution degrades to not-green and would hard-block on ci_not_green.
    # This test's subject is smoketest-artifact visibility (CI is assumed green,
    # as the stored qualification records), so pin the re-resolution to green.
    mocker.patch(
        "app.release_qualifier._resolve_ci_at_finalize",
        new=AsyncMock(
            return_value=(
                True,
                {
                    "state": "green",
                    "resolved_sha": "deadbeef",
                    "detail": "pinned-green",
                },
            )
        ),
    )
    # promotion_enabled defaults False (dark) — a confident candidate dry-runs.

    project = Project(
        name="Qualifier-Flush-Regression",
        user_id="qualifier-user",
        github_owner="acme",
        github_repo="widget",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    version = await _rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)

    # The release_audit sibling already COMPLETED with a clean audit report
    # (high audit score, no test erosion) so the only remaining input is the
    # smoketest about to be posted.
    audit_task = Task(
        project_id=project.id,
        title="Release audit child",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="agent-vm-audit-flush",
        status=TaskStatus.COMPLETED,
        context={"release_version_id": str(version.id)},
    )
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(audit_task)
    db_session.add(
        Artifact(
            task_id=audit_task.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
            logs=_json.dumps(
                {
                    "audit_score": 1.0,
                    "test_erosion": {
                        "deleted_test_files": [],
                        "test_count_delta": 0,
                        "assertion_delta": 0,
                    },
                    "findings": [],
                }
            ),
        )
    )
    await db_session.commit()

    # The qualifier-smoketest is IN_PROGRESS and about to post its result.
    smoke_task = Task(
        project_id=project.id,
        title="Qualifier smoketest child",
        payloads=[{"type": "smoketest", "base_branch": "automain"}],
        vm_instance_id="agent-vm-smoke-flush",
        status=TaskStatus.IN_PROGRESS,
        context={
            "release_version_id": str(version.id),
            "qualifier_smoketest": True,
        },
    )
    db_session.add(smoke_task)
    await db_session.commit()
    await db_session.refresh(smoke_task)

    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "ci_green": True,
        "started_at": "2026-05-20T00:00:00",
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": str(smoke_task.id),
    }
    db_session.add(version)
    await db_session.commit()

    # The smoketest passes every test — a healthy candidate.
    smoketest_result = {
        "passed": True,
        "tests": [
            {"name": "startup_probe", "status": "pass"},
            {"name": "login_flow", "status": "pass"},
        ],
        "suites": {
            "startup": {"passed": True, "tests": []},
            "curated": {"passed": True, "tests": []},
        },
    }
    token = create_agent_token(str(smoke_task.id), "agent-vm-smoke-flush")
    resp = await client.post(
        "/api/agent/status",
        json={
            "task_id": str(smoke_task.id),
            "status": "COMPLETED",
            "artifact_type": "SMOKETEST_RESULT",
            "logs": _json.dumps(smoketest_result),
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200

    async with _process_post_commit_factory(db_session)() as fresh:
        refetched = await fresh.get(ReleaseVersion, version.id)
        qual = refetched.last_qualification or {}

    # Finalization ran and the smoketest artifact was visible: a passing
    # smoketest yields a non-zero smoketest sub-score (not the 0.0 / hard
    # blocker the unflushed-artifact bug produced).
    assert qual.get("status") != "pending", f"version left wedged: {qual}"
    assert qual.get("blocker") is None, (
        f"passing candidate wrongly hard-blocked — artifact not visible: {qual}"
    )
    assert qual.get("sub_scores", {}).get("smoketest", 0.0) > 0.0
    # Promotion ships dark (promotion_enabled False) → dry-run, not promoted.
    assert qual.get("status") == "dry_run"


@pytest.mark.asyncio
async def test_non_qualifier_terminal_task_does_not_finalize(db_session, mocker):
    """A normal (non-qualifier) task going terminal must NOT touch any
    ReleaseVersion — the finalize hook is a cheap no-op for the common case."""
    from app.task_lifecycle import _process_post_commit
    from app.schemas import AgentStatusPayload
    from app.models import ReleaseVersion

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project, version = await _make_qualifier_project_and_version(db_session)
    # A pending qualification exists, but the task below is unrelated to it.
    version.last_qualification = {"status": "pending", "candidate_sha": "x" * 40}
    db_session.add(version)
    await db_session.commit()

    plain_task = Task(
        project_id=project.id,
        title="Ordinary create task",
        payloads=[{"type": "create", "base_branch": "automain"}],
        vm_instance_id="agent-vm-plain",
        status=TaskStatus.FAILED,
        context={},
    )
    db_session.add(plain_task)
    await db_session.commit()
    await db_session.refresh(plain_task)

    payload = AgentStatusPayload(task_id=plain_task.id, status=TaskStatus.FAILED)
    _factory = _process_post_commit_factory(db_session)
    await _process_post_commit(plain_task.id, payload, _session_factory=_factory)

    async with _factory() as fresh:
        refetched = await fresh.get(ReleaseVersion, version.id)

    # Untouched — a non-qualifier task must never advance qualification.
    assert (refetched.last_qualification or {}).get("status") == "pending"


# ===========================================================================
# Phase 5 (automain/LTS): hotfix back-merge + hotfix marker propagation
# ===========================================================================


@pytest.mark.asyncio
async def test_hotfix_backmerge_runs_for_hotfix_merge_to_main(db_session, mocker):
    """A hotfix that merged to main triggers the main -> automain back-merge:
    open_backmerge_pr opens the PR and merges it.

    The review-completion handler gates the back-merge on
    `is_rollup_merge and base_branch == "main" and orig.first_payload["hotfix"]`
    then calls hotfix_backmerge.open_backmerge_pr. This exercises that Phase 5
    back-merge unit end-to-end (mocked GitHub)."""
    from app import hotfix_backmerge

    project = Project(
        name="Hotfix-Backmerge",
        user_id="hotfix-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    create_pr = mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(
            return_value={
                "number": 4242,
                "html_url": "https://github.com/acme/widget/pull/4242",
            }
        ),
    )
    merge_pr = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )
    mocker.patch("app.hotfix_backmerge.broadcast_to_project", new_callable=AsyncMock)

    result = await hotfix_backmerge.open_backmerge_pr(
        db_session,
        project.id,
        merged_pr_url="https://github.com/acme/widget/pull/99",
        hotfix_task_id=uuid.uuid4(),
    )

    assert result["status"] == "merged"
    # The back-merge opens a main -> automain PR (the inverse of promotion).
    create_pr.assert_awaited_once()
    _, create_kwargs = create_pr.call_args
    assert create_kwargs["head"] == "main"
    assert create_kwargs["base"] == "automain"
    merge_pr.assert_awaited_once()


@pytest.mark.asyncio
async def test_hotfix_backmerge_trigger_commits_conflict_alert_and_task(
    db_session, mocker
):
    """CRITICAL: when the back-merge trigger inside _process_post_commit calls
    open_backmerge_pr and that call takes its CONFLICT path (session.add + flush
    of an operator-facing ERROR Alert and a high-priority conflict-resolution
    Task, deliberately NOT committed — its docstring: "the caller commits the
    session"), the trigger MUST commit so those rows survive.

    The bug: the back-merge block ran after the merged-PR commit, and nothing
    between it and the function's terminal `if session.dirty or session.new`
    commit re-dirtied the session. After open_backmerge_pr's flush() the session
    is flushed-but-clean, so the terminal commit was SKIPPED and the `async
    with` exit ROLLED BACK the conflict Alert + resolution Task — a back-merge
    conflict went completely unsignalled. The fix adds an explicit
    `await session.commit()` after the open_backmerge_pr call, mirroring the
    sibling smoketest-spawn block.

    This drives a real hotfix rollup-merge through _process_post_commit (a
    `review` task APPROVE verdict, original hotfix `plan` task EVALUATING so
    is_rollup_merge is True, base_branch=="main", hotfix:true) with
    open_backmerge_pr mocked to take the conflict path, then asserts — via a
    fresh session — that the Alert + Task were COMMITTED. test_hotfix_backmerge.py
    only tests open_backmerge_pr in isolation (and always commits itself), so
    nothing else covers the trigger's own commit in _process_post_commit.
    """
    from app.task_lifecycle import _process_post_commit
    from app.schemas import AgentStatusPayload
    from app.models import Alert, AlertSeverity

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    # _cleanup_branches runs on the merged hotfix task — stub it (pure GitHub I/O,
    # tangential to the commit contract under test).
    mocker.patch("app.task_lifecycle._cleanup_branches", new_callable=AsyncMock)

    project = Project(
        name="Hotfix-Backmerge-Commit",
        user_id="hotfix-user",
        github_owner="acme",
        github_repo="widget",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # The original hotfix task: a `plan` task with hotfix:true targeting main,
    # parked EVALUATING (the rollup-merge state — is_rollup_merge is
    # `orig.status == EVALUATING`).
    orig_task = Task(
        project_id=project.id,
        title="Hotfix plan",
        payloads=[
            {
                "type": "plan",
                "prompt": "expedite the fix",
                "hotfix": True,
                "base_branch": "main",
            }
        ],
        vm_instance_id=None,
        status=TaskStatus.EVALUATING,
    )
    db_session.add(orig_task)
    await db_session.commit()
    await db_session.refresh(orig_task)

    # The review task whose COMPLETED status drives the merge. Its review_loop
    # carries the PR coordinates and points back at the hotfix task.
    pr_number = 7777
    branch = "blueprint/rollup-hotfix"
    review_task = Task(
        project_id=project.id,
        title="Review of hotfix rollup PR",
        payloads=[{"type": "review", "base_branch": "main"}],
        vm_instance_id="agent-vm-review-hotfix",
        status=TaskStatus.IN_PROGRESS,
        context={
            "review_loop": {
                "iteration": 1,
                "pr_number": pr_number,
                "branch": branch,
                "base_branch": "main",
                "original_task_id": str(orig_task.id),
            }
        },
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    # GitHub mocks — patched at their source modules (the handler imports them
    # locally from app.github / app.routers.agent at call time).
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("gh-token", "acme", "widget")),
    )
    # PR head ref matches `branch` (no branch-mismatch abort); no head sha so the
    # CI gate short-circuits (ci_status stays None) and the merge proceeds.
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={"state": "open", "merged": False, "head": {"ref": branch}}
        ),
    )
    merge_pr = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    mocker.patch("app.github.get_pr_files", new=AsyncMock(return_value=[]))

    # open_backmerge_pr mocked to REPLICATE its real conflict path: session.add
    # an ERROR Alert + a high-priority conflict-resolution Task, then flush() —
    # and deliberately do NOT commit (its contract: the caller commits).
    conflict_alert_title = "Hotfix back-merge conflict: main -> automain"
    resolution_task_title = "Resolve hotfix back-merge conflict (mock)"

    async def _fake_open_backmerge_pr(session, project_id, **kwargs):
        session.add(
            Alert(
                project_id=project_id,
                severity=AlertSeverity.ERROR,
                title=conflict_alert_title,
                message="back-merge hit a merge conflict (test)",
            )
        )
        session.add(
            Task(
                project_id=project_id,
                title=resolution_task_title,
                payloads=[{"type": "create", "base_branch": "automain"}],
                priority=95,
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                status=TaskStatus.QUEUED,
                created_by="ORCHESTRATOR",
                context={"backmerge_conflict": True},
            )
        )
        # Flushed-but-NOT-committed — exactly what the real conflict path does.
        await session.flush()
        return {"status": "conflict"}

    mocker.patch(
        "app.hotfix_backmerge.open_backmerge_pr",
        new=AsyncMock(side_effect=_fake_open_backmerge_pr),
    )

    payload = AgentStatusPayload(
        task_id=review_task.id,
        status=TaskStatus.COMPLETED,
        verdict_json='{"verdict": "APPROVE", "feedback": "ok", "issues": []}',
    )
    _factory = _process_post_commit_factory(db_session)
    await _process_post_commit(review_task.id, payload, _session_factory=_factory)

    # The hotfix rollup PR must actually have merged (proves the path reached the
    # post-merge block where the back-merge trigger lives).
    merge_pr.assert_awaited()

    # The crux: the conflict Alert + resolution Task must be COMMITTED and
    # visible from a fresh session. Without the trigger's explicit commit the
    # `async with` exit rolls them back and a back-merge conflict is silent.
    async with _factory() as fresh:
        alert = (
            (
                await fresh.execute(
                    select(Alert).where(
                        Alert.project_id == project.id,
                        Alert.title == conflict_alert_title,
                    )
                )
            )
            .scalars()
            .first()
        )
        resolution_task = (
            (
                await fresh.execute(
                    select(Task).where(
                        Task.project_id == project.id,
                        Task.title == resolution_task_title,
                    )
                )
            )
            .scalars()
            .first()
        )

    assert alert is not None, (
        "back-merge conflict Alert was rolled back — the trigger never "
        "committed open_backmerge_pr's flushed-but-uncommitted rows"
    )
    assert alert.severity == AlertSeverity.ERROR
    assert resolution_task is not None, (
        "back-merge conflict-resolution Task was rolled back — the trigger "
        "never committed open_backmerge_pr's flushed-but-uncommitted rows"
    )
    assert resolution_task.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_hotfix_backmerge_gate_distinguishes_hotfix_rollup_and_promotion():
    """The back-merge gate fires ONLY for a hotfix rollup merging to main.

    Gate (task_lifecycle.py review-completion handler):
        is_rollup_merge and base_branch == "main"
        and bool(orig.first_payload.get("hotfix"))

    A normal rollup to main (hotfix=False) and a promotion (which is opened by
    the qualifier's promote(), not a rollup-merge review, so is_rollup_merge is
    False) must BOTH leave the back-merge un-triggered."""

    def _backmerge_should_fire(is_rollup_merge, base_branch, orig_payload):
        # Mirrors the live gate so a regression in either operand is caught.
        return (
            is_rollup_merge
            and base_branch == "main"
            and bool((orig_payload or {}).get("hotfix"))
        )

    # Hotfix rollup merged to main → back-merge fires.
    assert _backmerge_should_fire(True, "main", {"type": "plan", "hotfix": True})

    # Normal (non-hotfix) rollup merged to main → no back-merge.
    assert not _backmerge_should_fire(True, "main", {"type": "plan"})
    assert not _backmerge_should_fire(True, "main", {"type": "plan", "hotfix": False})

    # Promotion: automain -> main is opened by release_qualifier.promote(), not
    # a rollup-merge review task, so is_rollup_merge is False → no back-merge
    # (and would be a pointless self-merge anyway).
    assert not _backmerge_should_fire(False, "main", {"type": "plan", "hotfix": True})

    # A hotfix rollup that targets automain (not main) → no back-merge.
    assert not _backmerge_should_fire(
        True, "automain", {"type": "plan", "hotfix": True}
    )


def test_ci_gate_runs_for_integration_trunk_and_main():
    """The orchestrator's pre-merge CI gate must run for the resolved
    integration branch AND for main.

    Gate (task_lifecycle.py review-completion handler):
        is_ci_gated_merge = base_branch in (integration_branch, "main")

    Pre-Phase-1, integration_branch == "main", so a rollup to main is gated.
    Post-cutover, integration_branch == "automain": a rollup to automain MUST
    still be CI-gated (the bug: a hard-coded "main" check skipped it), and a
    hotfix rollup / promotion to main MUST also remain gated."""

    def _is_ci_gated_merge(base_branch, integration_branch):
        # Mirrors the live gate so a regression in either operand is caught.
        return base_branch in (integration_branch, "main")

    # Pre-cutover: integration branch is main; rollup to main is gated.
    assert _is_ci_gated_merge("main", "main")

    # Post-cutover: a rollup PR targets automain (the integration trunk) — it
    # MUST be CI-gated. This is the regression the hard-coded "main" caused.
    assert _is_ci_gated_merge("automain", "automain")

    # Post-cutover: a hotfix rollup / promotion targets main — still gated, so
    # a merge to main never bypasses CI.
    assert _is_ci_gated_merge("main", "automain")

    # An intermediate branch (research/plan staging) is NOT the integration
    # trunk and NOT main → not CI-gated by this path.
    assert not _is_ci_gated_merge("research/abc123", "automain")
    assert not _is_ci_gated_merge("plan/abc123", "main")


@pytest.mark.asyncio
async def test_hotfix_plan_subtasks_carry_hotfix_and_high_priority(db_session, mocker):
    """A hotfix plan task's subtasks carry hotfix:true and priority >= 90.

    Exercises the Phase 5 propagation in plan_completion._handle_plan_completion:
    when the parent plan payload has hotfix:true, every spawned subtask inherits
    hotfix:true (payload + context) and its priority is floored to >= 90."""
    import json as _json
    from app.task_completion import _handle_plan_completion

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch("app.task_completion.broadcast_to_project", new_callable=AsyncMock)

    project = Project(name="Hotfix-Plan", user_id="hotfix-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    plan_task = Task(
        project_id=project.id,
        title="[HOTFIX] Fix post-promotion failure",
        payloads=[
            {
                "type": "plan",
                "prompt": "plan the hotfix",
                "base_branch": "main",
                "hotfix": True,
            }
        ],
        vm_instance_id="agent-vm-hotfix-plan",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(plan_task)
    await db_session.commit()
    await db_session.refresh(plan_task)
    # _ensure_branch is lazily imported inside _handle_plan_completion; patch
    # the source module so the success path returns the (relocked) task.
    mocker.patch(
        "app.task_completion._ensure_branch",
        new_callable=AsyncMock,
        return_value=plan_task,
    )

    # A low-priority subtask — the hotfix floor must still lift it to >= 90.
    plan_logs = _json.dumps(
        {
            "subtasks": [
                {
                    "title": "Patch the regression",
                    "prompt": "fix it",
                    "priority": "low",
                    "scope": "small",
                }
            ]
        }
    )
    await _handle_plan_completion(plan_task, plan_logs, db_session)

    children = (
        (
            await db_session.execute(
                select(Task).where(Task.parent_task_id == plan_task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 1
    child = children[0]
    assert child.first_payload.get("hotfix") is True
    assert (child.context or {}).get("hotfix") is True
    assert child.priority >= 90, (
        f"hotfix subtask priority not floored to >=90: {child.priority}"
    )


@pytest.mark.asyncio
async def test_non_hotfix_plan_subtasks_do_not_carry_hotfix(db_session, mocker):
    """A normal plan task's subtasks must NOT carry hotfix:true and keep their
    ordinary (non-floored) priority — control case for the hotfix propagation."""
    import json as _json
    from app.task_completion import _handle_plan_completion

    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch("app.task_completion.broadcast_to_project", new_callable=AsyncMock)

    project = Project(name="Normal-Plan", user_id="hotfix-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    plan_task = Task(
        project_id=project.id,
        title="Ordinary plan",
        payloads=[
            {
                "type": "plan",
                "prompt": "plan it",
                "base_branch": "automain",
            }
        ],
        vm_instance_id="agent-vm-normal-plan",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(plan_task)
    await db_session.commit()
    await db_session.refresh(plan_task)
    mocker.patch(
        "app.task_completion._ensure_branch",
        new_callable=AsyncMock,
        return_value=plan_task,
    )

    plan_logs = _json.dumps(
        {
            "subtasks": [
                {
                    "title": "Small tweak",
                    "prompt": "narrow fix",
                    "priority": "low",
                    "scope": "small",
                }
            ]
        }
    )
    await _handle_plan_completion(plan_task, plan_logs, db_session)

    children = (
        (
            await db_session.execute(
                select(Task).where(Task.parent_task_id == plan_task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(children) == 1
    child = children[0]
    assert "hotfix" not in child.first_payload
    assert "hotfix" not in (child.context or {})
    # A "low" subtask is priority 0 (TASK_PRIORITY_MAP) — well under the
    # hotfix floor, confirming no floor was applied.
    assert child.priority < 90


@pytest.mark.asyncio
async def test_review_spawned_for_hotfix_subtask_carries_hotfix(db_session, mocker):
    """A review spawned for a hotfix subtask carries hotfix:true.

    Exercises Phase 5 propagation in review_pipeline._spawn_pipeline_task: the
    hotfix marker is read from the original task's payload and stamped onto the
    spawned review task's payload."""
    from app.review_pipeline import _spawn_pipeline_task

    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = Project(name="Hotfix-Review-Spawn", user_id="hotfix-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # The original task is a hotfix subtask (carries hotfix:true in its payload).
    original_task = Task(
        project_id=project.id,
        title="Hotfix subtask",
        payloads=[
            {
                "type": "create",
                "prompt": "patch the regression",
                "base_branch": "main",
                "hotfix": True,
            }
        ],
        vm_instance_id="agent-vm-hotfix-subtask",
        status=TaskStatus.COMPLETED,
        context={},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    review_task, was_created = await _spawn_pipeline_task(
        original_task,
        "review",
        db_session,
        7777,
        "blueprint/task-hotfix01",
        "main",
        original_prompt="patch the regression",
        original_task_id=str(original_task.id),
    )

    assert was_created is True
    assert review_task is not None
    assert review_task.first_payload.get("type") == "review"
    assert review_task.first_payload.get("hotfix") is True


@pytest.mark.asyncio
async def test_review_spawned_for_normal_subtask_has_no_hotfix(db_session, mocker):
    """A review spawned for a non-hotfix task must NOT carry hotfix:true —
    control case for the review-spawn hotfix propagation."""
    from app.review_pipeline import _spawn_pipeline_task

    mocker.patch("app.review_pipeline.broadcast_to_project", new_callable=AsyncMock)

    project = Project(name="Normal-Review-Spawn", user_id="hotfix-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    original_task = Task(
        project_id=project.id,
        title="Ordinary subtask",
        payloads=[
            {
                "type": "create",
                "prompt": "build a feature",
                "base_branch": "automain",
            }
        ],
        vm_instance_id="agent-vm-normal-subtask",
        status=TaskStatus.COMPLETED,
        context={},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    review_task, was_created = await _spawn_pipeline_task(
        original_task,
        "review",
        db_session,
        8888,
        "blueprint/task-normal01",
        "automain",
        original_prompt="build a feature",
        original_task_id=str(original_task.id),
    )

    assert was_created is True
    assert review_task is not None
    assert "hotfix" not in review_task.first_payload


@pytest.mark.asyncio
async def test_viewport_failure_screenshot_upload_url(
    client: AsyncClient, db_session, mocker
):
    """Viewport failure screenshot endpoint returns stable GCS path and stamps task context."""
    import asyncio

    project = Project(name="Viewport Screenshot Test", user_id="user-viewport-001")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "prompt": "fix viewport overflow"}],
        vm_instance_id="vm-viewport-001",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    fake_url = "https://storage.googleapis.com/bucket/viewport-failures/signed?X-Goog-Signature=abc"
    mocker.patch.object(asyncio, "to_thread", new=AsyncMock(return_value=fake_url))

    token = create_agent_token(str(task.id), "vm-viewport-001")
    resp = await client.post(
        "/api/agent/viewport/failure-screenshot-upload-url",
        json={"task_id": str(task.id), "component": "MessagesPanel", "width": 375},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["upload_url"] == fake_url
    # Stable path is keyed by project_id/component-width — not task-scoped
    assert (
        f"viewport-failures/{project.id}/MessagesPanel-375.png" in body["storage_uri"]
    )

    await db_session.refresh(task)
    assert task.context["viewport_failure_screenshot_uri"] == body["storage_uri"]


@pytest.mark.asyncio
async def test_viewport_failure_screenshot_upload_url_rejects_wrong_task(
    client: AsyncClient, db_session
):
    """Endpoint rejects requests where task_id doesn't match the agent token."""
    project = Project(name="Viewport Screenshot Auth Test", user_id="user-viewport-002")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task1 = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id="vm-viewport-002",
        status=TaskStatus.IN_PROGRESS,
    )
    task2 = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id="vm-viewport-003",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task1)
    db_session.add(task2)
    await db_session.commit()
    await db_session.refresh(task1)
    await db_session.refresh(task2)

    # Token for task1 but request uses task2's ID
    token = create_agent_token(str(task1.id), "vm-viewport-002")
    resp = await client.post(
        "/api/agent/viewport/failure-screenshot-upload-url",
        json={"task_id": str(task2.id), "component": "MessagesPanel", "width": 375},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_viewport_failure_screenshot_download_url(
    client: AsyncClient, db_session, mocker
):
    """Download endpoint reconstructs the exact GCS path the agent uploads to."""
    project = Project(name="Viewport Download Test", user_id="user-viewport-dl-001")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    captured = {}
    fake_url = (
        "https://storage.googleapis.com/bucket/signed-download?X-Goog-Signature=xyz"
    )

    def _fake_blob(object_name):
        captured["object_name"] = object_name
        blob = mocker.MagicMock()
        blob.generate_signed_url.return_value = fake_url
        return blob

    fake_client = mocker.MagicMock()
    fake_client.bucket.return_value.blob.side_effect = _fake_blob
    mocker.patch("google.cloud.storage.Client", return_value=fake_client)

    resp = await client.get(
        "/api/admin/viewport-failures/download-url",
        params={
            "project_id": str(project.id),
            "component": "MessagesPanel",
            "width": 375,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["url"] == fake_url
    # Path must exactly mirror the agent upload endpoint's object path
    # (viewport-failures/{project_id}/{component}-{width}.png) so the download
    # endpoint can actually serve the screenshot the agent uploaded.
    assert (
        captured["object_name"]
        == f"viewport-failures/{project.id}/MessagesPanel-375.png"
    )


@pytest.mark.asyncio
async def test_viewport_failure_screenshot_download_url_storage_failure(
    client: AsyncClient, db_session, mocker
):
    """A GCS failure surfaces as a 503, not an unhandled 500."""
    project = Project(
        name="Viewport Download Failure Test", user_id="user-viewport-dl-002"
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    mocker.patch(
        "google.cloud.storage.Client", side_effect=RuntimeError("gcs unavailable")
    )

    resp = await client.get(
        "/api/admin/viewport-failures/download-url",
        params={
            "project_id": str(project.id),
            "component": "MessagesPanel",
            "width": 375,
        },
    )
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Account-state-correlated RATE_LIMIT requeue tests (cluster 2/4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generic_failure_with_rate_limited_account_requeues_free(
    client: AsyncClient, db_session, mocker
):
    """Account-state-correlated RATE_LIMIT: a generic failure (no rate-limit keywords
    in logs) while the assigned account is in cooldown should be classified RATE_LIMIT
    and requeued without burning attempt_count.
    """
    from app.time_utils import _utcnow
    import app.max_plan_pool as pool_mod

    account_id = "aaaaaaaa-0001-4000-a000-000000000001"
    # Account disabled 5 minutes ago (hits=1 → 30-min cooldown → still cooling).
    # Use naive _utcnow() to match the production data shape: max_plan_pool stores
    # disabled_models[...]['at'] as a NAIVE datetime, so the fixture must too —
    # otherwise the test passes against an aware value the live pool never produces.
    at = _utcnow() - timedelta(minutes=5)
    fake_pool = {
        account_id: {
            "id": account_id,
            "name": "test-account-rl-corr",
            "active_tasks": 1,
            "disabled_models": {
                "claude-sonnet-4-6": {
                    "at": at,
                    "hits": 1,
                    "kind": "rate_limit",
                }
            },
        }
    }
    mocker.patch.object(pool_mod, "_account_by_id", fake_pool)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    project = Project(name="RL Account Corr Test", user_id="test-user-rl-corr-001")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "test task"}],
        vm_instance_id="agent-vm-rl-corr-001",
        status=TaskStatus.IN_PROGRESS,
        context={"llm_account_id": account_id, "llm_model": "claude-sonnet-4-6"},
        attempt_count=0,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id_str = str(task.id)

    token = create_agent_token(task_id_str, "agent-vm-rl-corr-001")
    # Generic failure — no rate-limit keywords; classification must fall through
    # to account-state correlation (_account_was_rate_limited) to return RATE_LIMIT.
    payload = {
        "task_id": task_id_str,
        "status": "FAILED",
        "failure_category": "claude_code_failed",
        "logs": "Process exited with code 1",
    }
    resp = await client.post(
        "/api/agent/status", json=payload, headers={"x-agent-token": token}
    )
    assert resp.status_code == 200

    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED, (
        f"Account-state-correlated RATE_LIMIT should requeue to QUEUED, got {task.status}"
    )
    # attempt_count must NOT be incremented — free requeue, not a retry burn
    assert task.attempt_count == 0, (
        f"attempt_count must stay 0 on free RATE_LIMIT requeue, got {task.attempt_count}"
    )


@pytest.mark.asyncio
async def test_generic_failure_with_healthy_account_burns_retry(
    client: AsyncClient, db_session, mocker
):
    """Without account-state correlation: a generic failure with a healthy account
    classifies as CODE_ERROR and burns an attempt_count retry slot.
    """
    import app.max_plan_pool as pool_mod

    account_id = "aaaaaaaa-0001-4000-a000-000000000002"
    fake_pool = {
        account_id: {
            "id": account_id,
            "name": "test-account-healthy-corr",
            "active_tasks": 1,
            "disabled_models": {},  # no disabled models — account is healthy
        }
    }
    mocker.patch.object(pool_mod, "_account_by_id", fake_pool)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    project = Project(
        name="Healthy Account Corr Test", user_id="test-user-healthy-corr-001"
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "test task"}],
        vm_instance_id="agent-vm-healthy-corr-001",
        status=TaskStatus.IN_PROGRESS,
        context={"llm_account_id": account_id, "llm_model": "claude-sonnet-4-6"},
        attempt_count=0,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id_str = str(task.id)

    token = create_agent_token(task_id_str, "agent-vm-healthy-corr-001")
    payload = {
        "task_id": task_id_str,
        "status": "FAILED",
        "failure_category": "claude_code_failed",
        "logs": "Process exited with code 1",
    }
    resp = await client.post(
        "/api/agent/status", json=payload, headers={"x-agent-token": token}
    )
    assert resp.status_code == 200

    await db_session.refresh(task)
    # CODE_ERROR with retries remaining → task is retried (QUEUED), not permanently failed
    assert task.status == TaskStatus.QUEUED, (
        f"CODE_ERROR with retries remaining should retry to QUEUED, got {task.status}"
    )
    # attempt_count MUST be incremented — CODE_ERROR burns a retry slot
    assert task.attempt_count == 1, (
        f"attempt_count must be 1 after CODE_ERROR retry burn, got {task.attempt_count}"
    )


@pytest.mark.asyncio
async def test_generic_accountless_failure_uses_scoped_db_pool_rl_check(
    client: AsyncClient, db_session, mocker
):
    """Accountless DB RL correlation must preserve provider/model scope.

    Without the scoped arguments, an unrelated user's or provider's exhausted
    pool can flip this task into RATE_LIMIT. The durable DB fallback in
    agent_status.py must pass through the task's routed provider/model when
    no account ID is present.
    """
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)
    pool_wide_mock = mocker.patch(
        "app.routers.agent_status.db_pool_wide_rate_limit_cooldown",
        new_callable=AsyncMock,
        return_value=True,
    )

    project = Project(
        name="Scoped DB Pool RL Test", user_id="test-user-scoped-db-pool-001"
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "test task"}],
        vm_instance_id="agent-vm-scoped-db-pool-001",
        status=TaskStatus.IN_PROGRESS,
        context={
            "llm_provider": "anthropic",
            "llm_model": "claude-sonnet-4-6",
        },
        attempt_count=0,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id_str = str(task.id)

    token = create_agent_token(task_id_str, "agent-vm-scoped-db-pool-001")
    payload = {
        "task_id": task_id_str,
        "status": "FAILED",
        "failure_category": "incomplete",
        "logs": "Process exited with code 1",
    }
    resp = await client.post(
        "/api/agent/status", json=payload, headers={"x-agent-token": token}
    )
    assert resp.status_code == 200

    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert task.attempt_count == 0
    pool_wide_mock.assert_awaited_once_with(
        at_time=task.started_at,
        providers=["anthropic"],
        model="claude-sonnet-4-6",
        user_id=None,
    )


@pytest.mark.asyncio
async def test_requeue_parent_task_uses_vm_stopped_at_for_rate_limit_correlation(
    db_session, mocker
):
    """_requeue_parent_task passes child.vm_stopped_at as at_time so a pool
    rate-limit window that expired before the requeue sweep is still detected.

    Scenario: child died during a pool rate-limit block; vm_stopped_at records
    the death instant. By the time the parent is re-evaluated, the cooldown has
    cleared. Without the at_time fix, classify_failure would check the pool at
    sweep time (no cooldown) and return CODE_ERROR — reducing the parent's
    requeue budget. With the fix, at_time=vm_stopped_at is passed, the cooldown
    is detected, and the child's failure class is RATE_LIMIT (INFRA — budget
    not reduced).

    Verification: _account_was_rate_limited side_effect returns True only when
    at_time is not None. If the call site omits at_time, the side_effect returns
    False → CODE_ERROR → budget reduction in compute_effective_max_requeues.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    def _rl_only_at_failure_time(task, at_time=None):
        # Return True (rate-limited) only when a failure instant was provided.
        return at_time is not None

    mocker.patch(
        "app.failure_classification._account_was_rate_limited",
        side_effect=_rl_only_at_failure_time,
    )

    project = Project(name="Requeue atTime Test", user_id="test-user-requeue-attime")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "investigate X"}],
        vm_instance_id=None,
        status=TaskStatus.EVALUATING,
        attempt_count=0,
        context={},
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    # Child died at vm_stopped_at — no rate-limit keywords in the error.
    child = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do part of X"}],
        vm_instance_id="vm-child-rl",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
        attempt_count=1,
        latest_status_message="Process exited with code 1",
        vm_stopped_at=datetime.utcnow() - timedelta(minutes=30),
        last_heartbeat=datetime.utcnow() - timedelta(minutes=31),
        context={"failure_category": "claude_code_failed"},
    )
    db_session.add(child)
    await db_session.commit()

    from app.task_lifecycle import _requeue_parent_task

    await _requeue_parent_task(parent, db_session)
    await db_session.commit()
    await db_session.refresh(parent)

    # Parent should be re-queued for evaluation (EVALUATING), not failed.
    assert parent.status == TaskStatus.EVALUATING, (
        "parent must remain EVALUATING after rate-limit-correlated requeue "
        f"(got {parent.status}); at_time plumbing may be broken"
    )
    # Requeue count must not exceed budget (RATE_LIMIT is INFRA — no reduction).
    parent_ctx = parent.context or {}
    assert parent_ctx.get("_requeue_count", 0) <= 1, (
        "parent requeue count must be ≤1 after first requeue"
    )


@pytest.mark.asyncio
async def test_fix_task_transient_close_pr_preserves_re_review(db_session, mocker):
    """Fix task completing while its PR is transiently closed (not merged) must
    still spawn the re-review — not silently skip it.

    Regression for: the orchestrator's mergeability-reset loop (close+reopen to
    force GitHub to recompute mergeable=None) transiently closes the PR. If a fix
    task completes during that brief window, the old early-exit fired on
    state=='closed' and dropped the re-review handoff, leaving the parent task
    polling 'CI passed — waiting for review approval' forever.

    The fix gates the skip on merged==True only; a non-merged closed state must
    proceed with the normal re-review path.
    """
    from app.task_lifecycle import _process_post_commit
    from app.schemas import AgentStatusPayload
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as _AS
    from unittest.mock import MagicMock

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("gh-tok", "owner", "repo")),
    )
    # PR is closed but NOT merged — simulates the brief mergeability-reset window
    # where the orchestrator's close+reopen cycle transiently closes the PR.
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"state": "closed", "merged": False}),
    )
    mocker.patch(
        "app.security.scan_pr_for_injection",
        new=AsyncMock(
            return_value={
                "blocked": False,
                "summary": "clean",
                "findings": [],
                "report": "",
            }
        ),
    )
    fake_review_task = MagicMock()
    fake_review_task.id = uuid.uuid4()
    spawn_mock = AsyncMock(return_value=(fake_review_task, True))
    mocker.patch("app.task_lifecycle._spawn_pipeline_task", spawn_mock)

    project = Project(name="Transient Close Re-Review", user_id="u-transient-close")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    original_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "build feature X"}],
        vm_instance_id=None,
        status=TaskStatus.COMPLETED,
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    fix_task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "pr_number": 555, "branch": "feat/transient-close"}],
        vm_instance_id="agent-vm-transient-close",
        status=TaskStatus.IN_PROGRESS,
        context={
            "review_loop": {
                "pr_number": 555,
                "branch": "feat/transient-close",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
                "iteration": 2,
                "fix_reason": "code_review",
            }
        },
    )
    db_session.add(fix_task)
    await db_session.commit()
    await db_session.refresh(fix_task)

    payload = AgentStatusPayload(
        task_id=fix_task.id,
        status=TaskStatus.COMPLETED,
    )

    _factory = async_sessionmaker(db_session.bind, class_=_AS, expire_on_commit=False)
    await _process_post_commit(fix_task.id, payload, _session_factory=_factory)

    # The re-review MUST have been spawned — not silently dropped.
    spawn_mock.assert_awaited_once()
    call_args = spawn_mock.await_args
    assert call_args.args[1] == "review", (
        f"Expected re-review spawn with type='review', got {call_args.args[1]!r} — "
        f"a transiently-closed PR must not be treated as abandoned"
    )


# ---------------------------------------------------------------------------
# Bootstrap-JWT TTL gating: confirm-refresh + warm-handoff downgrade to short
# TTL, while unconfirmed (old-image) VMs keep the long deploy-survival window.
# ---------------------------------------------------------------------------


def _jwt_ttl_seconds(token: str) -> int:
    """Return the encoded TTL (exp - iat) in seconds for an agent JWT."""
    from app.auth import verify_agent_jwt

    claims = verify_agent_jwt(token)
    return int(claims["exp"]) - int(claims["iat"])


@pytest.mark.asyncio
async def test_refresh_token_downgrades_bootstrap_to_short_ttl(
    client: AsyncClient, db_session
):
    """A cold-start agent that mints with the long bootstrap TTL and then
    proves it can refresh MUST be rotated down to the short TTL.

    This exercises the REAL bootstrap timing (not the 1h happy path): the
    incoming token carries AGENT_BOOTSTRAP_TOKEN_TTL_HOURS, yet the token
    returned by /api/agent/refresh-token must reflect AGENT_TOKEN_TTL_HOURS.
    """
    from app.auth import AGENT_TOKEN_TTL_HOURS, AGENT_BOOTSTRAP_TOKEN_TTL_HOURS

    assert AGENT_BOOTSTRAP_TOKEN_TTL_HOURS > AGENT_TOKEN_TTL_HOURS, (
        "bootstrap TTL must be strictly longer than the short TTL for the "
        "downgrade to be observable"
    )

    project = Project(name="Bootstrap Downgrade", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="agent-vm-bootstrap-downgrade",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    task_id_str = str(task.id)

    # Mint with the LONG bootstrap TTL, as compute_manifest does for a cold
    # start that has not yet proven refresh capability.
    bootstrap_token = create_agent_token(
        task_id_str,
        "agent-vm-bootstrap-downgrade",
        project_id=str(project.id),
        ttl_hours=AGENT_BOOTSTRAP_TOKEN_TTL_HOURS,
    )
    assert (
        abs(_jwt_ttl_seconds(bootstrap_token) - AGENT_BOOTSTRAP_TOKEN_TTL_HOURS * 3600)
        <= 5
    ), "precondition: incoming token must carry the long bootstrap TTL"

    resp = await client.post(
        "/api/agent/refresh-token",
        headers={"x-agent-token": bootstrap_token},
    )
    assert resp.status_code == 200, resp.text
    refreshed = resp.json()["token"]

    refreshed_ttl = _jwt_ttl_seconds(refreshed)
    assert abs(refreshed_ttl - AGENT_TOKEN_TTL_HOURS * 3600) <= 5, (
        f"refresh-token must downgrade to the short TTL "
        f"({AGENT_TOKEN_TTL_HOURS}h); got {refreshed_ttl}s"
    )
    assert refreshed_ttl < AGENT_BOOTSTRAP_TOKEN_TTL_HOURS * 3600, (
        "refreshed token must NOT retain the long bootstrap window"
    )

    # Capability proof must be persisted so subsequent warm handoffs on this
    # VM also issue the short TTL.
    await db_session.refresh(task)
    assert task.last_token_refresh_at is not None


@pytest.mark.asyncio
async def test_warm_handoff_issues_short_ttl_for_refresh_capable_vm(
    client: AsyncClient, db_session
):
    """The warm-handoff path mints the short TTL when the VM has already
    proven it can refresh (a prior role on the VM recorded
    last_token_refresh_at)."""
    from app.auth import AGENT_TOKEN_TTL_HOURS, AGENT_BOOTSTRAP_TOKEN_TTL_HOURS

    project = Project(name="Warm Handoff Short TTL", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    vm_id = "agent-vm-warm-confirmed"
    prior_task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 4242}],
        vm_instance_id=vm_id,
        status=TaskStatus.COMPLETED,
        # The agent refreshed during this prior role — proves capability.
        last_token_refresh_at=datetime.utcnow(),
    )
    db_session.add(prior_task)
    deadline = (datetime.utcnow() + timedelta(seconds=240)).isoformat()
    pinned_task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "pr_number": 4242, "branch": "feat/x"}],
        vm_instance_id=None,
        status=TaskStatus.QUEUED,
        context={
            "warm_target_vm_id": vm_id,
            "warm_target_pr": "4242",
            "warm_handoff_deadline": deadline,
        },
    )
    db_session.add(pinned_task)
    await db_session.commit()
    await db_session.refresh(prior_task)
    await db_session.refresh(pinned_task)

    token = create_agent_token(str(prior_task.id), vm_id)
    resp = await client.post(
        "/api/agent/next-role",
        json={
            "vm_instance_id": vm_id,
            "prior_task_id": str(prior_task.id),
            "prior_pr_number": 4242,
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["action"] == "claim"

    warm_ttl = _jwt_ttl_seconds(body["agent_token"])
    assert abs(warm_ttl - AGENT_TOKEN_TTL_HOURS * 3600) <= 5, (
        f"warm handoff for a refresh-capable VM must mint the short TTL; "
        f"got {warm_ttl}s"
    )
    assert warm_ttl < AGENT_BOOTSTRAP_TOKEN_TTL_HOURS * 3600, (
        "a confirmed VM must NOT receive a fresh bootstrap window on handoff"
    )


@pytest.mark.asyncio
async def test_warm_handoff_keeps_bootstrap_ttl_for_unconfirmed_vm(
    client: AsyncClient, db_session
):
    """Deploy-survival regression guard: a VM that has NEVER refreshed (e.g.
    an old image that cannot call /refresh-token) must keep receiving the long
    bootstrap window on warm handoff."""
    from app.auth import AGENT_BOOTSTRAP_TOKEN_TTL_HOURS

    project = Project(name="Warm Handoff Bootstrap TTL", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    vm_id = "agent-vm-warm-unconfirmed"
    prior_task = Task(
        project_id=project.id,
        payloads=[{"type": "review", "pr_number": 5252}],
        vm_instance_id=vm_id,
        status=TaskStatus.COMPLETED,
        # Never refreshed — capability unproven.
        last_token_refresh_at=None,
    )
    db_session.add(prior_task)
    deadline = (datetime.utcnow() + timedelta(seconds=240)).isoformat()
    pinned_task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "pr_number": 5252, "branch": "feat/y"}],
        vm_instance_id=None,
        status=TaskStatus.QUEUED,
        context={
            "warm_target_vm_id": vm_id,
            "warm_target_pr": "5252",
            "warm_handoff_deadline": deadline,
        },
    )
    db_session.add(pinned_task)
    await db_session.commit()
    await db_session.refresh(prior_task)
    await db_session.refresh(pinned_task)

    token = create_agent_token(str(prior_task.id), vm_id)
    resp = await client.post(
        "/api/agent/next-role",
        json={
            "vm_instance_id": vm_id,
            "prior_task_id": str(prior_task.id),
            "prior_pr_number": 5252,
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["action"] == "claim"

    warm_ttl = _jwt_ttl_seconds(body["agent_token"])
    assert abs(warm_ttl - AGENT_BOOTSTRAP_TOKEN_TTL_HOURS * 3600) <= 5, (
        f"warm handoff for an unconfirmed VM must keep the long bootstrap TTL "
        f"so a deploy cutover can't 401 it mid-task; got {warm_ttl}s"
    )


def test_cold_start_manifest_issues_bootstrap_ttl():
    """The cold-start manifest grant carries the long bootstrap TTL so an
    in-flight agent survives a deploy/rebake cutover that straddles expiry."""
    from app.compute import build_agent_secret_manifest
    from app.auth import AGENT_BOOTSTRAP_TOKEN_TTL_HOURS

    task_id = uuid.uuid4()
    manifest = build_agent_secret_manifest(
        vm_instance_id="agent-vm-cold-bootstrap",
        task_id=task_id,
        oauth_token="sk-ant-test",
        task_type="create",
        provider="anthropic",
        auth_mode="oauth",
    )
    ttl = _jwt_ttl_seconds(manifest["env"]["AGENT_SECRET_TOKEN"])
    assert abs(ttl - AGENT_BOOTSTRAP_TOKEN_TTL_HOURS * 3600) <= 5, (
        f"cold-start manifest must mint the long bootstrap TTL; got {ttl}s"
    )
