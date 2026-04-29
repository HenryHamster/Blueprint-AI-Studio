"""Tests for the warm-VM continuity admin endpoint (/api/admin/warm-vm-state).

Surfaces operator visibility for the soft warm-VM lifecycle. The endpoint
exposes which VMs are heartbeating, which task each is running, queued
warm-pinned tasks per VM, and the warm-pin clear-rate (a leading indicator
of warm-VM instability).
"""
import uuid
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.orm.attributes import flag_modified

from app.main import app
from app.models import Project, Task, TaskStatus
from app.auth import get_current_user, require_admin
import app.shared_state as _shared_state


@pytest.mark.asyncio
async def test_warm_vm_state_empty(client: AsyncClient, db_session):
    """Endpoint returns 200 + empty active_vms when no warm activity exists."""
    # Snapshot + clear in-memory state so other tests don't leak in.
    saved_hb = dict(_shared_state.vm_last_heartbeat)
    saved_clears = list(_shared_state.warm_pin_clear_events)
    _shared_state.vm_last_heartbeat.clear()
    _shared_state.warm_pin_clear_events.clear()
    try:
        resp = await client.get("/api/admin/warm-vm-state")
        assert resp.status_code == 200
        body = resp.json()
        assert body["active_vms"] == []
        assert body["warm_pin_metrics"]["clears_last_hour"] == 0
        assert body["warm_pin_metrics"]["clears_last_24h"] == 0
        assert body["warm_pin_metrics"]["active_pinned_queued_total"] == 0
        assert body["warm_pin_metrics"]["active_warm_chains"] == 0
        assert "warm_continuity_enabled" in body
        assert isinstance(body["next_role_poll_timeout_secs"], int)
    finally:
        _shared_state.vm_last_heartbeat.clear()
        _shared_state.vm_last_heartbeat.update(saved_hb)
        _shared_state.warm_pin_clear_events.clear()
        _shared_state.warm_pin_clear_events.extend(saved_clears)


@pytest.mark.asyncio
async def test_warm_vm_state_active_chain(client: AsyncClient, db_session):
    """A live warm chain shows up: heartbeating VM, IN_PROGRESS warm-start task,
    pinned QUEUED follow-up, and prior completion in the recent list."""
    saved_hb = dict(_shared_state.vm_last_heartbeat)
    saved_clears = list(_shared_state.warm_pin_clear_events)
    _shared_state.vm_last_heartbeat.clear()
    _shared_state.warm_pin_clear_events.clear()

    project = Project(name="Warm-VM Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    vm_id = "agent-vm-warm-test"
    _shared_state.record_vm_heartbeat(vm_id)

    # Prior task completed within the last hour, recent_completions should
    # surface it.
    prior = Task(
        project_id=project.id,
        title="Review PR #42",
        payloads=[{"type": "review", "pr_number": 42}],
        vm_instance_id=vm_id,
        status=TaskStatus.COMPLETED,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=5),
    )
    db_session.add(prior)

    # IN_PROGRESS warm-start task — should be the current_task.
    current = Task(
        project_id=project.id,
        title="Fix PR #42",
        payloads=[{"type": "fix", "pr_number": 42}],
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
        started_at=datetime.utcnow(),
        last_heartbeat=datetime.utcnow(),
        context={
            "_timing": {
                "warm_start": True,
                "prior_role_vm_id": vm_id,
                "setup_secs": 4.2,
            }
        },
    )
    db_session.add(current)

    # QUEUED warm-pinned follow-up — should appear in warm_pinned_queued.
    deadline = (datetime.utcnow() + timedelta(seconds=240)).isoformat()
    pinned = Task(
        project_id=project.id,
        title="Re-review PR #42",
        payloads=[{"type": "review", "pr_number": 42}],
        vm_instance_id=None,
        status=TaskStatus.QUEUED,
        context={
            "warm_target_vm_id": vm_id,
            "warm_target_pr": 42,
            "warm_handoff_deadline": deadline,
        },
    )
    db_session.add(pinned)

    # A clear-out event from a different VM — should bump the clears metric.
    _shared_state.record_warm_pin_clear("agent-vm-other", "deadline_expired")

    await db_session.commit()
    try:
        resp = await client.get("/api/admin/warm-vm-state")
        assert resp.status_code == 200
        body = resp.json()
        # The endpoint surfaces our test VM at minimum (other tests may share
        # state, so we look it up by id rather than asserting count).
        ours = next((v for v in body["active_vms"] if v["vm_instance_id"] == vm_id), None)
        assert ours is not None, f"vm {vm_id} missing from {body['active_vms']}"
        assert ours["is_alive"] is True
        assert ours["last_heartbeat"] is not None

        cur = ours["current_task"]
        assert cur is not None
        assert cur["task_type"] == "fix"
        assert cur["pr_number"] == 42
        assert cur["warm_start"] is True
        assert cur["prior_role_vm_id"] == vm_id
        assert cur["setup_secs"] == 4.2

        assert len(ours["warm_pinned_queued"]) == 1
        pinned_out = ours["warm_pinned_queued"][0]
        assert pinned_out["task_type"] == "review"
        assert pinned_out["warm_target_pr"] == 42
        assert pinned_out["warm_handoff_deadline"] == deadline

        assert any(c["task_type"] == "review" for c in ours["recent_completions"])

        assert body["warm_pin_metrics"]["active_pinned_queued_total"] >= 1
        assert body["warm_pin_metrics"]["active_warm_chains"] >= 1
        assert body["warm_pin_metrics"]["clears_last_hour"] >= 1
    finally:
        _shared_state.vm_last_heartbeat.clear()
        _shared_state.vm_last_heartbeat.update(saved_hb)
        _shared_state.warm_pin_clear_events.clear()
        _shared_state.warm_pin_clear_events.extend(saved_clears)


@pytest.mark.asyncio
async def test_warm_vm_state_stale_heartbeat(client: AsyncClient, db_session):
    """A VM with a stale heartbeat is reported as not-alive."""
    saved_hb = dict(_shared_state.vm_last_heartbeat)
    _shared_state.vm_last_heartbeat.clear()
    try:
        vm_id = "agent-vm-stale"
        # Heartbeat 5 minutes ago — well past the 90s freshness threshold.
        _shared_state.vm_last_heartbeat[vm_id] = datetime.utcnow() - timedelta(minutes=5)

        resp = await client.get("/api/admin/warm-vm-state")
        assert resp.status_code == 200
        ours = next(v for v in resp.json()["active_vms"] if v["vm_instance_id"] == vm_id)
        assert ours["is_alive"] is False
        assert ours["last_heartbeat"] is not None
    finally:
        _shared_state.vm_last_heartbeat.clear()
        _shared_state.vm_last_heartbeat.update(saved_hb)


@pytest.mark.asyncio
async def test_warm_vm_state_requires_admin(override_get_session, db_session):
    """Non-admin user is rejected with 403."""
    from fastapi import HTTPException

    async def non_admin():
        return {"user_id": "other", "email": "other@example.com", "role": "user"}

    async def reject_admin():
        raise HTTPException(status_code=403, detail="Admin privileges required")

    app.dependency_overrides[get_current_user] = non_admin
    app.dependency_overrides[require_admin] = reject_admin
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/admin/warm-vm-state")
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(require_admin, None)
