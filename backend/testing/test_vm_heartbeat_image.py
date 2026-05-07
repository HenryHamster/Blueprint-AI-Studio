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
