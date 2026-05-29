"""Tests for the agent strategic-context fetch sub-router
(app.routers.agent_strategic_fetch): read-only direction/playbook/gap
fetch endpoints, their auth/cross-project semantics, history capping,
pending_source visibility, and archive lineage.

Relocated from test_agent.py when the strategic fetch surface was extracted
into its own router seam; behavior and contracts are unchanged.
"""

import pytest
from httpx import AsyncClient

from app.models import Project, Task, TaskStatus
from app.auth import create_agent_token


@pytest.mark.asyncio
async def test_agent_fetch_direction_returns_full_record_with_history_cap(
    client: AsyncClient, db_session
):
    """Agent gets the full direction including capped research_history."""
    project = Project(
        name="Fetch Test",
        user_id="test-user-123",
        directions=[
            {
                "id": "dir-1",
                "status": "active",
                "text": "First",
                "priority": "high",
                "research_history": [{"prs_merged": i} for i in range(30)],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-fetch")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-fetch")
    res = await client.get(
        f"/api/agent/projects/{project.id}/directions/dir-1",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    assert body["data"]["id"] == "dir-1"
    assert body["data"]["text"] == "First"
    # 30 entries → capped at 20 most recent
    assert len(body["data"]["research_history"]) == 20
    assert body["data"]["_history_truncated"] == {"shown": 20, "total": 30}


@pytest.mark.asyncio
async def test_agent_fetch_direction_rejects_cross_project(
    client: AsyncClient, db_session
):
    """Agent with token for project A can't fetch direction in project B (404,
    not 403, so probing can't be used to enumerate project ids)."""
    project_a = Project(name="A", user_id="test-user-a")
    project_b = Project(
        name="B",
        user_id="test-user-b",
        directions=[{"id": "dir-b1", "status": "active", "text": "B"}],
    )
    db_session.add_all([project_a, project_b])
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    task_a = Task(project_id=project_a.id, payloads=[{}], vm_instance_id="agent-vm-a")
    db_session.add(task_a)
    await db_session.commit()
    await db_session.refresh(task_a)

    token = create_agent_token(str(task_a.id), "agent-vm-a")
    res = await client.get(
        f"/api/agent/projects/{project_b.id}/directions/dir-b1",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_agent_fetch_playbook_entry_with_and_without_prefix(
    client: AsyncClient, db_session
):
    """Agent can fetch a playbook entry by either bare key or playbook/<key>."""
    project = Project(
        name="Playbook Test",
        user_id="test-user-pb",
        design_doc={
            "playbook/frontend-build": {
                "title": "Frontend build",
                "content": "Run npm run build before pushing.",
                "confirmed_count": 5,
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-pb")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-pb")
    headers = {"x-agent-token": token}

    # Bare key
    r1 = await client.get(
        f"/api/agent/projects/{project.id}/playbook/frontend-build",
        headers=headers,
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["data"]["entry"]["confirmed_count"] == 5

    # 404 on missing key
    r2 = await client.get(
        f"/api/agent/projects/{project.id}/playbook/does-not-exist",
        headers=headers,
    )
    assert r2.status_code == 404


@pytest.mark.asyncio
async def test_agent_fetch_playbook_entry_pending_source_visibility(
    client: AsyncClient, db_session
):
    """pending_source visibility rules on the fetch-by-key endpoint:
    - owner task can fetch its own pending entry
    - peer task gets 404 for that same pending key
    - archived entries (playbook_archive) are never returned
    - entries without pending_source are returned to any task in the project
    """
    project = Project(
        name="Playbook Pending Source Test",
        user_id="test-user-pps",
        design_doc={},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create the owner task first so we have its real id
    owner_task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="agent-vm-owner",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(owner_task)
    await db_session.commit()
    await db_session.refresh(owner_task)

    from sqlalchemy.orm.attributes import flag_modified

    project.design_doc = {
        "playbook/pending-entry": {
            "content": "pending guidance",
            "pending_source": str(owner_task.id),
            "confirmed_count": 0,
        },
        "playbook/committed-entry": {
            "content": "committed guidance",
            "confirmed_count": 1,
        },
        "playbook_archive": [
            {
                "key": "playbook/old-archived",
                "content": "archived guidance",
            }
        ],
    }
    flag_modified(project, "design_doc")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Peer task (different id, same project)
    peer_task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="agent-vm-peer",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(peer_task)
    await db_session.commit()
    await db_session.refresh(peer_task)

    owner_token = create_agent_token(str(owner_task.id), "agent-vm-owner")
    peer_token = create_agent_token(str(peer_task.id), "agent-vm-peer")
    owner_headers = {"x-agent-token": owner_token}
    peer_headers = {"x-agent-token": peer_token}

    # Owner can fetch its own pending entry
    r_owner = await client.get(
        f"/api/agent/projects/{project.id}/playbook/pending-entry",
        headers=owner_headers,
    )
    assert r_owner.status_code == 200, r_owner.text
    assert r_owner.json()["data"]["entry"]["content"] == "pending guidance"

    # Peer gets 404 for the pending entry
    r_peer = await client.get(
        f"/api/agent/projects/{project.id}/playbook/pending-entry",
        headers=peer_headers,
    )
    assert r_peer.status_code == 404, r_peer.text

    # Both tasks can fetch committed (non-pending) entry
    r_committed_owner = await client.get(
        f"/api/agent/projects/{project.id}/playbook/committed-entry",
        headers=owner_headers,
    )
    assert r_committed_owner.status_code == 200
    r_committed_peer = await client.get(
        f"/api/agent/projects/{project.id}/playbook/committed-entry",
        headers=peer_headers,
    )
    assert r_committed_peer.status_code == 200

    # Archived entries are not exposed via this endpoint
    r_archived = await client.get(
        f"/api/agent/projects/{project.id}/playbook/old-archived",
        headers=owner_headers,
    )
    assert r_archived.status_code == 404, r_archived.text


@pytest.mark.asyncio
async def test_agent_fetch_gap_handles_active_and_archived(
    client: AsyncClient, db_session
):
    """Agent can fetch gaps regardless of active/archived status; the response
    flags ``from_archive`` so the agent knows the context."""
    project = Project(
        name="Gap Test",
        user_id="test-user-gap",
        problem_model={
            "gaps": [
                {
                    "id": "g-active",
                    "name": "Active gap",
                    "history": [f"h{i}" for i in range(10)],
                },
            ],
            "archived_gaps": [
                {
                    "id": "g-old",
                    "name": "Closed gap",
                    "history": [f"h{i}" for i in range(5)],
                },
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-gap")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-gap")
    headers = {"x-agent-token": token}

    r1 = await client.get(
        f"/api/agent/projects/{project.id}/gaps/g-active", headers=headers
    )
    assert r1.status_code == 200, r1.text
    body = r1.json()
    assert body["data"]["id"] == "g-active"
    assert body["from_archive"] is False
    assert len(body["data"]["history"]) == 10  # full history, not trimmed

    r2 = await client.get(
        f"/api/agent/projects/{project.id}/gaps/g-old", headers=headers
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["from_archive"] is True

    r3 = await client.get(
        f"/api/agent/projects/{project.id}/gaps/g-missing", headers=headers
    )
    assert r3.status_code == 404
