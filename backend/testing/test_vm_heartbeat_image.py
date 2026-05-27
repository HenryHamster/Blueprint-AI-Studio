"""Heartbeat -> compute._vm_images integration test.

Regression test for the cluster that wires per-VM image identity through
the agent heartbeat path. Previously the agent posted vm_image fields to
/api/agent/vm-heartbeat but the schema declared only vm_instance_id with
extra='ignore', so the image fields were silently dropped and
get_stale_image_vms() always returned an empty list. This test asserts
the field names line up end-to-end and that record_vm_image is invoked.
"""

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
