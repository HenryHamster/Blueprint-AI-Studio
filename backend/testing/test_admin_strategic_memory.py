from __future__ import annotations

import pytest

from app.models import MessageSurface, Project, ProjectMember, ProjectMessage


@pytest.mark.asyncio
async def test_owner_explicitly_resumes_bounded_memory_backfill(
    client, db_session, monkeypatch
):
    project = Project(name="memory-backfill-api", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        ProjectMember(
            project_id=project.id,
            user_id="test-user-123",
            role="owner",
        )
    )
    for sequence in range(1, 102):
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                sequence=sequence,
                surface=MessageSurface.CONVERSATION,
                from_type="user",
                actor_user_id="test-user-123",
                content=f"historical fact {sequence}",
            )
        )
    await db_session.commit()

    status_response = await client.get(
        f"/api/admin/projects/{project.id}/memory/backfill"
    )
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "not_started"

    disabled = await client.post(
        f"/api/admin/projects/{project.id}/memory/backfill/advance",
        json={"expected_cursor_id": None, "expected_last_sequence": 0},
    )
    assert disabled.status_code == 409

    monkeypatch.setenv("STRATEGIC_MEMORY_V2_PROJECT_IDS", str(project.id))
    monkeypatch.setenv("STRATEGIC_MEMORY_V2_BACKFILL_ENABLED", "true")
    first = await client.post(
        f"/api/admin/projects/{project.id}/memory/backfill/advance",
        json={"expected_cursor_id": None, "expected_last_sequence": 0},
    )
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["status"] == "running"
    assert first_body["page"]["scanned"] == 100
    assert first_body["page"]["complete"] is False
    assert first_body["page"]["queued_job_id"] is None
    assert first_body["cutoff_sequence"] == 101
    assert first_body["last_sequence"] == 100

    stale = await client.post(
        f"/api/admin/projects/{project.id}/memory/backfill/advance",
        json={"expected_cursor_id": None, "expected_last_sequence": 0},
    )
    assert stale.status_code == 409

    second = await client.post(
        f"/api/admin/projects/{project.id}/memory/backfill/advance",
        json={
            "expected_cursor_id": first_body["cursor_id"],
            "expected_last_sequence": first_body["last_sequence"],
        },
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["status"] == "complete"
    assert second_body["page"]["scanned"] == 1
    assert second_body["page"]["complete"] is True
    assert second_body["last_sequence"] == 101


@pytest.mark.asyncio
async def test_memory_backfill_is_project_owner_scoped(client, db_session, monkeypatch):
    foreign = Project(name="foreign-memory", user_id="different-owner")
    db_session.add(foreign)
    await db_session.commit()
    monkeypatch.setenv("STRATEGIC_MEMORY_V2_PROJECT_IDS", str(foreign.id))
    monkeypatch.setenv("STRATEGIC_MEMORY_V2_BACKFILL_ENABLED", "true")

    status_response = await client.get(
        f"/api/admin/projects/{foreign.id}/memory/backfill"
    )
    trigger_response = await client.post(
        f"/api/admin/projects/{foreign.id}/memory/backfill/advance",
        json={"expected_cursor_id": None, "expected_last_sequence": 0},
    )
    assert status_response.status_code == 404
    assert trigger_response.status_code == 404
