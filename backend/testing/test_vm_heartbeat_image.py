"""Heartbeat -> compute._vm_images integration test.

Regression test for the cluster that wires per-VM image identity through
the agent heartbeat path. Previously the agent posted vm_image fields to
/api/agent/vm-heartbeat but the schema declared only vm_instance_id with
extra='ignore', so the image fields were silently dropped and
get_stale_image_vms() always returned an empty list. This test asserts
the field names line up end-to-end and that record_vm_image is invoked.
"""

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from app.auth import create_agent_token
from app.models import Project, Task, TaskStatus


@pytest.mark.asyncio
async def test_vm_heartbeat_records_image_identity(client: AsyncClient, db_session):
    """vm-heartbeat with image fields populates compute._vm_images."""
    import app.compute as _compute
    import app.shared_state as _shared_state

    project = Project(name="VM-Heartbeat Image", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id="agent-vm-warm-image",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _shared_state.vm_last_heartbeat.pop("agent-vm-warm-image", None)
    _compute._vm_images.pop("agent-vm-warm-image", None)

    token = create_agent_token(str(task.id), "agent-vm-warm-image")
    resp = await client.post(
        "/api/agent/vm-heartbeat",
        json={
            "vm_instance_id": "agent-vm-warm-image",
            "vm_image_name": "agent-vm-2026-05-07-abc123",
            "vm_image_build_ts": 1746547200,
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 204

    image_name, build_ts = _compute.get_vm_image("agent-vm-warm-image")
    assert image_name == "agent-vm-2026-05-07-abc123"
    assert build_ts == 1746547200


@pytest.mark.asyncio
async def test_vm_heartbeat_without_image_fields_is_no_op_for_images(
    client: AsyncClient, db_session
):
    """Heartbeat without image fields must not clobber prior recorded image."""
    import app.compute as _compute

    project = Project(name="VM-Heartbeat No Image", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id="agent-vm-warm-noimg",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _compute.record_vm_image("agent-vm-warm-noimg", "prebaked-image", 1700000000)
    token = create_agent_token(str(task.id), "agent-vm-warm-noimg")
    resp = await client.post(
        "/api/agent/vm-heartbeat",
        json={"vm_instance_id": "agent-vm-warm-noimg"},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 204

    image_name, build_ts = _compute.get_vm_image("agent-vm-warm-noimg")
    assert image_name == "prebaked-image"
    assert build_ts == 1700000000


@pytest.mark.asyncio
async def test_vm_heartbeat_records_runtime_fingerprint_and_exposes_stale_vm(
    client: AsyncClient, db_session
):
    """vm-heartbeat persists agent_runtime_fingerprint into stale-image diagnostics."""
    import app.compute as _compute

    vm_id = "agent-vm-runtime-vm-heartbeat"
    runtime_fingerprint = "a" * 64

    project = Project(name="VM-Heartbeat Runtime Fingerprint", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _compute._vm_images.pop(vm_id, None)
    _compute._vm_runtime_fingerprints.pop(vm_id, None)

    token = create_agent_token(str(task.id), vm_id)
    resp = await client.post(
        "/api/agent/vm-heartbeat",
        json={
            "vm_instance_id": vm_id,
            "vm_image_name": "agent-vm-2026-05-07-abc123",
            "vm_image_build_ts": 1,
            "agent_runtime_fingerprint": runtime_fingerprint,
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 204

    assert _compute.get_vm_runtime_fingerprint(vm_id) == runtime_fingerprint
    stale_vms = _compute.get_stale_image_vms(threshold_hours=0)
    assert any(
        vm["vm_id"] == vm_id
        and vm["image_name"] == "agent-vm-2026-05-07-abc123"
        and vm["agent_runtime_fingerprint"] == runtime_fingerprint
        for vm in stale_vms
    )


@pytest.mark.asyncio
async def test_task_heartbeat_records_runtime_fingerprint_and_exposes_stale_vm(
    client: AsyncClient, db_session
):
    """/api/agent/heartbeat persists agent_runtime_fingerprint into stale-image diagnostics."""
    import app.compute as _compute

    vm_id = "agent-vm-runtime-task-heartbeat"
    runtime_fingerprint = "b" * 64

    project = Project(
        name="Task Heartbeat Runtime Fingerprint", user_id="test-user-123"
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _compute._vm_images.pop(vm_id, None)
    _compute._vm_runtime_fingerprints.pop(vm_id, None)

    token = create_agent_token(str(task.id), vm_id)
    resp = await client.post(
        "/api/agent/heartbeat",
        json={
            "task_id": str(task.id),
            "vm_image_name": "agent-vm-2026-05-07-def456",
            "vm_image_build_ts": 1,
            "agent_runtime_fingerprint": runtime_fingerprint,
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200

    assert _compute.get_vm_runtime_fingerprint(vm_id) == runtime_fingerprint
    stale_vms = _compute.get_stale_image_vms(threshold_hours=0)
    assert any(
        vm["vm_id"] == vm_id
        and vm["image_name"] == "agent-vm-2026-05-07-def456"
        and vm["agent_runtime_fingerprint"] == runtime_fingerprint
        for vm in stale_vms
    )


@pytest.mark.asyncio
async def test_vm_heartbeat_records_source_sha(client: AsyncClient, db_session):
    """vm-heartbeat persists agent_source_sha into compute._vm_source_shas."""
    import app.compute as _compute

    vm_id = "agent-vm-source-sha-vm-heartbeat"
    source_sha = "abc1234def5678"

    project = Project(name="VM-Heartbeat Source SHA", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _compute._vm_source_shas.pop(vm_id, None)

    token = create_agent_token(str(task.id), vm_id)
    resp = await client.post(
        "/api/agent/vm-heartbeat",
        json={"vm_instance_id": vm_id, "agent_source_sha": source_sha},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 204
    assert _compute.get_vm_source_sha(vm_id) == source_sha


@pytest.mark.asyncio
async def test_task_heartbeat_records_source_sha(client: AsyncClient, db_session):
    """/api/agent/heartbeat persists agent_source_sha into compute._vm_source_shas."""
    import app.compute as _compute

    vm_id = "agent-vm-source-sha-task-heartbeat"
    source_sha = "0123456789abcdef0123456789abcdef01234567"

    project = Project(name="Task Heartbeat Source SHA", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _compute._vm_source_shas.pop(vm_id, None)

    token = create_agent_token(str(task.id), vm_id)
    resp = await client.post(
        "/api/agent/heartbeat",
        json={"task_id": str(task.id), "agent_source_sha": source_sha},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200
    assert _compute.get_vm_source_sha(vm_id) == source_sha


@pytest.mark.asyncio
async def test_task_heartbeat_persists_runner_agent_source_sha(
    client: AsyncClient, db_session
):
    """/api/agent/heartbeat on a runner-pinned task persists agent_source_sha
    onto the durable LocalRunner row (the checkout-runner fleet's serving
    provenance), independent of the warm-pool flag."""
    import app.compute as _compute
    from app.models import LocalRunner

    project = Project(name="Runner Heartbeat SHA", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner = LocalRunner(
        project_id=project.id,
        token_hash="a" * 64,
        status="active",
    )
    db_session.add(runner)
    await db_session.commit()
    await db_session.refresh(runner)

    vm_id = f"runner:{runner.id}"
    source_sha = "9" * 40
    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _compute._vm_source_shas.pop(vm_id, None)
    token = create_agent_token(str(task.id), vm_id)
    resp = await client.post(
        "/api/agent/heartbeat",
        json={"task_id": str(task.id), "agent_source_sha": source_sha},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200
    assert _compute.get_vm_source_sha(vm_id) == source_sha

    await db_session.refresh(runner)
    assert runner.agent_source_sha == source_sha


def test_record_and_get_vm_source_sha_roundtrip():
    """compute.record_vm_source_sha / get_vm_source_sha round-trip and clear."""
    import app.compute as _compute

    vm_id = "agent-vm-source-sha-unit"
    _compute._vm_source_shas.pop(vm_id, None)
    assert _compute.get_vm_source_sha(vm_id) is None
    _compute.record_vm_source_sha(vm_id, "deadbeef1234")
    assert _compute.get_vm_source_sha(vm_id) == "deadbeef1234"
    # Empty vm id is a no-op, never stored under "".
    _compute.record_vm_source_sha("", "cafe1234")
    assert _compute.get_vm_source_sha("") is None
    # Heartbeat-state clear drops the recorded SHA alongside the other maps.
    _compute._clear_vm_heartbeat_state(vm_id)
    assert _compute.get_vm_source_sha(vm_id) is None


def test_get_image_source_git_sha_local_mode_returns_none():
    """get_image_source_git_sha is a safe no-op in LOCAL_MODE / for empty names."""
    import app.compute as _compute

    # The test suite runs in LOCAL_MODE, so the GCP path is never hit.
    assert _compute.get_image_source_git_sha("blueprint-agent-v2-123") is None
    assert _compute.get_image_source_git_sha("") is None


@pytest.mark.asyncio
async def test_persist_pool_vm_image_state_updates_pool_member(db_session):
    """Warm-pool members can durably mirror observed image/source identity."""
    import app.compute as _compute
    from app.models import PoolVm, PoolVmState, Project

    project = Project(name="Pool VM Durable Image State", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    member = PoolVm(
        vm_instance_id="agent-vm-pool-durable",
        project_id=project.id,
        state=PoolVmState.IDLE,
    )
    db_session.add(member)
    await db_session.commit()
    await db_session.refresh(member)

    updated = await _compute.persist_pool_vm_image_state(
        db_session,
        member.vm_instance_id,
        image_name="blueprint-agent-v2-20260606",
        build_ts=1749168000,
        runtime_fingerprint="f" * 64,
        source_sha="0123456789abcdef0123456789abcdef01234567",
    )
    await db_session.commit()
    await db_session.refresh(member)

    assert updated is True
    assert member.agent_image_name == "blueprint-agent-v2-20260606"
    assert member.agent_image_build_ts == 1749168000
    assert member.agent_runtime_fingerprint == "f" * 64
    assert member.agent_source_sha == "0123456789abcdef0123456789abcdef01234567"
    assert member.last_heartbeat_at is not None


@pytest.mark.asyncio
async def test_persist_runner_agent_source_sha_updates_localrunner(db_session):
    """Runner-pinned heartbeats durably mirror the agent source SHA onto the
    LocalRunner row so the checkout-runner fleet survives orchestrator restarts."""
    import app.compute as _compute
    from app.models import LocalRunner

    project = Project(name="Runner Durable SHA", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner = LocalRunner(
        project_id=project.id,
        token_hash="f" * 64,
        status="active",
    )
    db_session.add(runner)
    await db_session.commit()
    await db_session.refresh(runner)

    updated = await _compute.persist_runner_agent_source_sha(
        db_session,
        f"runner:{runner.id}",
        "0123456789abcdef0123456789abcdef01234567",
    )
    await db_session.commit()
    await db_session.refresh(runner)

    assert updated is True
    assert runner.agent_source_sha == "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.asyncio
async def test_persist_runner_agent_source_sha_ignores_vm_ids(db_session):
    """Non-runner vm ids, unresolvable runner ids, and absent SHAs are no-ops."""
    import app.compute as _compute

    assert (
        await _compute.persist_runner_agent_source_sha(
            db_session, "agent-vm-task-1", "c" * 40
        )
        is False
    )
    assert (
        await _compute.persist_runner_agent_source_sha(
            db_session, "runner:00000000-0000-0000-0000-000000000000", "c" * 40
        )
        is False
    )
    assert (
        await _compute.persist_runner_agent_source_sha(
            db_session, "runner:not-a-uuid", "c" * 40
        )
        is False
    )
    assert (
        await _compute.persist_runner_agent_source_sha(
            db_session, "runner:00000000-0000-0000-0000-000000000000", None
        )
        is False
    )


@pytest.mark.asyncio
async def test_agent_fleet_source_sha_snapshot_merges_pool_and_task_vms(db_session):
    """Snapshot groups live PoolVm rows and in-memory task VMs without double-counting."""
    import app.compute as _compute
    from app.models import PoolVm, PoolVmState

    project = Project(name="Fleet SHA Snapshot", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    pool_known = PoolVm(
        vm_instance_id="agent-vm-snapshot-pool-known",
        project_id=project.id,
        state=PoolVmState.IDLE,
        agent_source_sha="a" * 40,
        agent_image_name="agent-image-a",
        agent_image_build_ts=100,
        last_heartbeat_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
    )
    pool_unknown = PoolVm(
        vm_instance_id="agent-vm-snapshot-pool-unknown",
        project_id=project.id,
        state=PoolVmState.LEASED,
        agent_image_name="agent-image-legacy",
        agent_image_build_ts=90,
    )
    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id="agent-vm-snapshot-task",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(pool_known)
    db_session.add(pool_unknown)
    db_session.add(task)
    await db_session.commit()

    _compute.record_vm_source_sha("agent-vm-snapshot-pool-known", "b" * 40)
    _compute.record_vm_source_sha("agent-vm-snapshot-task", "c" * 40)
    _compute.record_vm_image("agent-vm-snapshot-task", "agent-image-c", 110)
    try:
        snapshot = await _compute.agent_fleet_source_sha_snapshot(
            db_session, project.id
        )
    finally:
        _compute._clear_vm_heartbeat_state("agent-vm-snapshot-pool-known")
        _compute._clear_vm_heartbeat_state("agent-vm-snapshot-task")

    assert snapshot["total_observed"] == 3
    assert snapshot["unknown_count"] == 1
    assert snapshot["mixed"] is True
    by_sha = {bucket["source_sha"]: bucket for bucket in snapshot["buckets"]}
    assert by_sha["a" * 40]["warm_pool_count"] == 1
    assert by_sha["a" * 40]["task_vm_count"] == 0
    assert by_sha["c" * 40]["task_vm_count"] == 1
    assert by_sha[None]["count"] == 1
