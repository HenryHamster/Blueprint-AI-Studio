"""Tests for GET /api/admin/tasks/{id}/serial-console endpoint.

Tests the on-demand serial console fetch for task VMs.
"""

import uuid
import pytest
from httpx import AsyncClient

from app.models import Project, ProjectMember, Task, TaskStatus


@pytest.mark.asyncio
async def test_serial_console_task_not_found(client: AsyncClient, db_session):
    """Returns 404 for non-existent task."""
    fake_id = str(uuid.uuid4())
    resp = await client.get(f"/api/admin/tasks/{fake_id}/serial-console")
    assert resp.status_code == 404
    data = resp.json()
    # Check error message in either format (detail or error.message)
    assert "Task not found" in data.get("detail", "") or "Task not found" in data.get(
        "error", {}
    ).get("message", "")


@pytest.mark.asyncio
async def test_serial_console_no_vm(client: AsyncClient, db_session):
    """Returns 404 for task without vm_instance_id."""
    project = Project(name="Test Project", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    member = ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    db_session.add(member)

    # Task with no VM
    task = Task(
        project_id=project.id,
        title="No VM Task",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        vm_instance_id=None,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    resp = await client.get(f"/api/admin/tasks/{task.id}/serial-console")
    assert resp.status_code == 404
    data = resp.json()
    assert "no vm_instance_id" in data.get(
        "detail", ""
    ) or "no vm_instance_id" in data.get("error", {}).get("message", "")


@pytest.mark.asyncio
async def test_serial_console_live_capture(
    client: AsyncClient, db_session, monkeypatch
):
    """Returns live serial console when VM exists and _capture_serial_for_vm succeeds."""
    project = Project(name="Test Project", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    member = ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    db_session.add(member)

    vm_id = "test-vm-123"
    zone = "us-central1-f"

    # Mock get_vm_zone (sync function)
    def mock_get_vm_zone(vid):
        return zone if vid == vm_id else "unknown"

    monkeypatch.setattr("app.admin.get_vm_zone", mock_get_vm_zone)

    # Mock _capture_serial_for_vm (sync function)
    test_serial_log = "Test serial output\nLine 2\nLine 3"

    def mock_capture_serial(vid, z):
        if vid == vm_id and z == zone:
            return test_serial_log
        return None

    monkeypatch.setattr("app.admin._capture_serial_for_vm", mock_capture_serial)

    # Mock get_vm_status (sync function)
    def mock_get_vm_status(vid, z):
        if vid == vm_id and z == zone:
            return "RUNNING"
        return None

    monkeypatch.setattr("app.admin.get_vm_status", mock_get_vm_status)

    task = Task(
        project_id=project.id,
        title="Test Task with VM",
        payloads=[{"type": "create"}],
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=vm_id,
        context={"provision_zone": zone},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    resp = await client.get(f"/api/admin/tasks/{task.id}/serial-console")
    assert resp.status_code == 200

    body = resp.json()
    assert body["vm_instance_id"] == vm_id
    assert body["zone"] == zone
    assert body["vm_status"] == "RUNNING"
    assert body["serial_log"] == test_serial_log
    assert body["source"] == "live"
    assert "captured_at" in body
    assert body["captured_at"]  # Should be a valid ISO-8601 string


@pytest.mark.asyncio
async def test_serial_console_cached_fallback(
    client: AsyncClient, db_session, monkeypatch
):
    """Falls back to cached serial log when live capture returns None."""
    project = Project(name="Test Project", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    member = ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    db_session.add(member)

    vm_id = "deleted-vm-456"
    zone = "us-central1-c"
    cached_serial = "Cached serial log from last reap"

    # Mock get_vm_zone (sync)
    def mock_get_vm_zone(vid):
        return zone if vid == vm_id else "unknown"

    monkeypatch.setattr("app.admin.get_vm_zone", mock_get_vm_zone)

    # Mock _capture_serial_for_vm (sync) to return None (VM deleted)
    def mock_capture_serial(vid, z):
        return None

    monkeypatch.setattr("app.admin._capture_serial_for_vm", mock_capture_serial)

    # Mock get_vm_status (sync)
    def mock_get_vm_status(vid, z):
        return "DELETED"  # VM is gone

    monkeypatch.setattr("app.admin.get_vm_status", mock_get_vm_status)

    task = Task(
        project_id=project.id,
        title="Task with Deleted VM",
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        vm_instance_id=vm_id,
        context={
            "provision_zone": zone,
            "last_serial_log": cached_serial,
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    resp = await client.get(f"/api/admin/tasks/{task.id}/serial-console")
    assert resp.status_code == 200

    body = resp.json()
    assert body["vm_instance_id"] == vm_id
    assert body["zone"] == zone
    assert body["vm_status"] == "DELETED"
    assert body["serial_log"] == cached_serial
    assert body["source"] == "cached"
    assert "captured_at" in body


@pytest.mark.asyncio
async def test_serial_console_no_cached_fallback(
    client: AsyncClient, db_session, monkeypatch
):
    """Returns None for serial_log when both live and cached are unavailable."""
    project = Project(name="Test Project", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    member = ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    db_session.add(member)

    vm_id = "lost-vm-789"
    zone = "us-east4-b"

    # Mock get_vm_zone (sync)
    def mock_get_vm_zone(vid):
        return zone if vid == vm_id else "unknown"

    monkeypatch.setattr("app.admin.get_vm_zone", mock_get_vm_zone)

    # Mock _capture_serial_for_vm (sync) to return None
    def mock_capture_serial(vid, z):
        return None

    monkeypatch.setattr("app.admin._capture_serial_for_vm", mock_capture_serial)

    # Mock get_vm_status (sync)
    def mock_get_vm_status(vid, z):
        return None  # API error or VM not found

    monkeypatch.setattr("app.admin.get_vm_status", mock_get_vm_status)

    # Task with no cached serial log
    task = Task(
        project_id=project.id,
        title="Task Lost Forever",
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        vm_instance_id=vm_id,
        context={"provision_zone": zone},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    resp = await client.get(f"/api/admin/tasks/{task.id}/serial-console")
    assert resp.status_code == 200

    body = resp.json()
    assert body["vm_instance_id"] == vm_id
    assert body["zone"] == zone
    assert body["vm_status"] is None
    assert body["serial_log"] is None
    assert body["source"] == "live"  # Tried live, but both live and cached are None
    assert "captured_at" in body


@pytest.mark.asyncio
async def test_serial_console_falls_back_to_context_provision_zone(
    client: AsyncClient, db_session, monkeypatch
):
    """When the in-process _vm_zones cache misses (e.g. multi-replica deploy
    where another replica provisioned the VM), the endpoint must fall back to
    task.context['provision_zone'] and use it for live capture and status."""
    project = Project(name="Test Project", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    member = ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    db_session.add(member)

    vm_id = "orphan-vm-999"
    fallback_zone = "us-east1-a"

    # Cache miss — get_vm_zone returns "unknown" (this replica didn't provision)
    monkeypatch.setattr("app.admin.get_vm_zone", lambda vid: "unknown")

    capture_calls: list[tuple[str, str | None]] = []

    def mock_capture_serial(vid, z):
        capture_calls.append((vid, z))
        return "serial: hello from us-east1-a"

    monkeypatch.setattr("app.admin._capture_serial_for_vm", mock_capture_serial)

    status_calls: list[tuple[str, str | None]] = []

    def mock_get_vm_status(vid, z):
        status_calls.append((vid, z))
        return "RUNNING"

    monkeypatch.setattr("app.admin.get_vm_status", mock_get_vm_status)

    task = Task(
        project_id=project.id,
        title="Task with Cache-Miss Zone",
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        vm_instance_id=vm_id,
        context={"provision_zone": fallback_zone},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    resp = await client.get(f"/api/admin/tasks/{task.id}/serial-console")
    assert resp.status_code == 200

    body = resp.json()
    assert body["vm_instance_id"] == vm_id
    # Fallback resolved — endpoint reports the context-provided zone
    assert body["zone"] == fallback_zone
    # vm_status was reachable because the zone was resolved
    assert body["vm_status"] == "RUNNING"
    assert body["serial_log"] == "serial: hello from us-east1-a"
    assert body["source"] == "live"

    # Both downstream calls saw the resolved fallback zone, not "unknown"
    assert capture_calls == [(vm_id, fallback_zone)]
    assert status_calls == [(vm_id, fallback_zone)]


@pytest.mark.asyncio
async def test_serial_console_unknown_zone_with_no_context_fallback(
    client: AsyncClient, db_session, monkeypatch
):
    """When zone cache misses AND task.context lacks provision_zone, the
    endpoint reports zone=null and skips the gated vm_status call."""
    project = Project(name="Test Project", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    member = ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    db_session.add(member)

    vm_id = "orphan-vm-no-ctx"

    monkeypatch.setattr("app.admin.get_vm_zone", lambda vid: "unknown")
    monkeypatch.setattr("app.admin._capture_serial_for_vm", lambda vid, z: None)

    def mock_get_vm_status(vid, z):
        raise AssertionError("Should not call get_vm_status with unknown zone")

    monkeypatch.setattr("app.admin.get_vm_status", mock_get_vm_status)

    task = Task(
        project_id=project.id,
        title="Task with Unknown Zone",
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        vm_instance_id=vm_id,
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    resp = await client.get(f"/api/admin/tasks/{task.id}/serial-console")
    assert resp.status_code == 200

    body = resp.json()
    assert body["vm_instance_id"] == vm_id
    assert body["zone"] is None
    assert body["vm_status"] is None
    assert body["serial_log"] is None
    assert body["source"] == "live"
