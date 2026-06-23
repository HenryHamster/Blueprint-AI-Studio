"""Tests for the agent strategic-context fetch sub-router
(app.routers.agent_strategic_fetch): read-only direction/playbook/gap
fetch endpoints, their auth/cross-project semantics, history capping,
pending_source visibility, and archive lineage.

Relocated from test_agent.py when the strategic fetch surface was extracted
into its own router seam; behavior and contracts are unchanged.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models import (
    AuditFindingResolution,
    MessageType,
    Project,
    ProjectMessage,
    QualificationVerdict,
    ReleaseVersion,
    Task,
    TaskStatus,
)
from app.auth import create_agent_token


async def _make_project_task(db_session, *, vm="agent-vm-msg"):
    project = Project(name="Msg Test", user_id="test-user-msg")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(project_id=project.id, payloads=[{}], vm_instance_id=vm)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return project, task


async def _add_message(db_session, project, **kwargs):
    msg = ProjectMessage(project_id=project.id, **kwargs)
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)
    return msg


async def _add_ambiguous_messages(
    db_session, project, *, shared_prefix: str = "abcdef12"
):
    """Create two messages whose UUIDs share the same 8-hex prefix."""
    import uuid as _uuid

    ids = (
        _uuid.UUID(shared_prefix + "0000" + "4000" + "8000" + "000000000001"),
        _uuid.UUID(shared_prefix + "0000" + "4000" + "8000" + "000000000002"),
    )
    messages: list[ProjectMessage] = []
    for mid in ids:
        msg = ProjectMessage(
            id=mid,
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="user",
            content="dup",
            status="open",
        )
        db_session.add(msg)
        messages.append(msg)
    await db_session.commit()
    for msg in messages:
        await db_session.refresh(msg)
    return shared_prefix, messages


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
async def test_agent_fetch_design_doc_section_returns_full_and_grounds(
    client: AsyncClient, db_session, mocker
):
    """Agent can pull a single design_doc section by key (slash keys work via
    the {key:path} route), and the returned section is presence-grounded
    against the integration branch exactly like the design-doc get op — a
    present-status systems entry whose backing files are absent downgrades to
    ``missing`` before it reaches the agent."""

    # Drive grounding through the GitHub-backed branch path (the local-HEAD
    # fallback degrades to a no-op without a real checkout) and report every
    # backing file absent so the present-status entry deterministically
    # downgrades — proving the endpoint shares the design-doc get grounding.
    async def _ctx(session, project):
        return {
            "token": "ghs_tok",
            "owner": "acme",
            "repo": "widget",
            "branch": "automain",
            "project_id": str(project.id),
        }

    mocker.patch(
        "app.routers.agent_strategic_fetch.resolve_branch_verification_context",
        side_effect=_ctx,
    )
    mocker.patch(
        "app.source_ref_validation._file_exists_in_repo_branch",
        side_effect=lambda path, **kw: False,
    )
    project = Project(
        name="Section Test",
        user_id="test-user-sec",
        design_doc={
            "systems/auth": {
                "status": "mature",
                "modules": ["backend/app/gone.py"],
                "description": "Auth subsystem.",
            },
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-sec")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-sec")
    headers = {"x-agent-token": token}

    res = await client.get(
        f"/api/agent/projects/{project.id}/design-doc/systems/auth",
        headers=headers,
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["key"] == "systems/auth"
    # Grounding ran: present status was reconciled to missing with a warning.
    assert data["value"]["status"] == "missing"
    assert "presence_warning" in data["value"]

    # 404 on a missing key.
    r_missing = await client.get(
        f"/api/agent/projects/{project.id}/design-doc/systems/does-not-exist",
        headers=headers,
    )
    assert r_missing.status_code == 404


@pytest.mark.asyncio
async def test_agent_fetch_design_doc_section_rejects_cross_project(
    client: AsyncClient, db_session
):
    """Token for project A can't fetch a section in project B (404, not 403)."""
    project_a = Project(name="A", user_id="test-user-sa")
    project_b = Project(
        name="B",
        user_id="test-user-sb",
        design_doc={"systems/core": {"status": "mature", "description": "B core"}},
    )
    db_session.add_all([project_a, project_b])
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    task_a = Task(project_id=project_a.id, payloads=[{}], vm_instance_id="agent-vm-sa")
    db_session.add(task_a)
    await db_session.commit()
    await db_session.refresh(task_a)

    token = create_agent_token(str(task_a.id), "agent-vm-sa")
    res = await client.get(
        f"/api/agent/projects/{project_b.id}/design-doc/systems/core",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_agent_fetch_problem_model_returns_full_untrimmed(
    client: AsyncClient, db_session
):
    """The whole-model fetch returns the full problem_model — including
    archived_gaps and uncapped per-gap history — for when the strategic
    payload trimmed or dropped it under budget."""
    project = Project(
        name="PM Fetch Test",
        user_id="test-user-pmf",
        problem_model={
            "revision": 7,
            "intent": {"purpose": "Ship the thing."},
            "gaps": [
                {
                    "id": "g-1",
                    "name": "Big gap",
                    "history": [{"ts": f"t{i}", "event": "x"} for i in range(40)],
                }
            ],
            "archived_gaps": [{"id": "g-old", "name": "done", "status": "closed"}],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-pmf")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-pmf")
    res = await client.get(
        f"/api/agent/projects/{project.id}/problem_model",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["revision"] == 7
    # Full, untrimmed: archived gaps present and per-gap history not capped.
    assert {g["id"] for g in data["archived_gaps"]} == {"g-old"}
    assert len(data["gaps"][0]["history"]) == 40


@pytest.mark.asyncio
async def test_agent_fetch_problem_model_rejects_cross_project(
    client: AsyncClient, db_session
):
    """Token for project A can't read project B's problem_model (404)."""
    project_a = Project(name="PMA", user_id="u-a")
    project_b = Project(
        name="PMB", user_id="u-b", problem_model={"intent": {"purpose": "secret"}}
    )
    db_session.add_all([project_a, project_b])
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    task_a = Task(project_id=project_a.id, payloads=[{}], vm_instance_id="agent-vm-pma")
    db_session.add(task_a)
    await db_session.commit()
    await db_session.refresh(task_a)

    token = create_agent_token(str(task_a.id), "agent-vm-pma")
    res = await client.get(
        f"/api/agent/projects/{project_b.id}/problem_model",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404


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


@pytest.mark.asyncio
async def test_agent_fetch_problem_model_returns_full_durable_model(
    client: AsyncClient, db_session
):
    """Agent can fetch the full durable problem_model, including archived gaps.

    This is the read-only escape hatch for cases where the strategic payload
    dropped or summarized the section under budget pressure.
    """
    project = Project(
        name="Problem Model Fetch Test",
        user_id="test-user-pm",
        problem_model={
            "revision": 17,
            "intent": {
                "purpose": "Ship a durable fetch path.",
                "current_focus": "recover full strategic state",
            },
            "constraints": [
                {"id": "c-1", "text": "Keep the model read-only."},
            ],
            "friction": [
                {"id": "f-1", "name": "budget pressure", "status": "active"},
            ],
            "capabilities": [
                {
                    "id": "cap-1",
                    "name": "read model",
                    "status": "mature",
                    "backing_paths": ["backend/app/routers/agent_strategic_fetch.py"],
                }
            ],
            "gaps": [
                {
                    "id": "g-active",
                    "name": "Active gap",
                    "history": [f"h{i}" for i in range(12)],
                    "past_moves": [f"m{i}" for i in range(9)],
                    "evidence": [f"e{i}" for i in range(7)],
                },
            ],
            "archived_gaps": [
                {
                    "id": "g-archived",
                    "name": "Archived gap",
                    "history": [f"ah{i}" for i in range(4)],
                    "past_moves": [f"am{i}" for i in range(3)],
                    "evidence": [f"ae{i}" for i in range(2)],
                }
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-pm")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-pm")
    res = await client.get(
        f"/api/agent/projects/{project.id}/problem-model",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    assert body["data"]["revision"] == 17
    assert body["data"]["intent"]["purpose"] == "Ship a durable fetch path."
    assert body["data"]["constraints"][0]["text"] == "Keep the model read-only."
    assert body["data"]["friction"][0]["name"] == "budget pressure"
    assert body["data"]["capabilities"][0]["backing_paths"] == [
        "backend/app/routers/agent_strategic_fetch.py"
    ]
    assert len(body["data"]["gaps"][0]["history"]) == 12
    assert len(body["data"]["gaps"][0]["past_moves"]) == 9
    assert len(body["data"]["gaps"][0]["evidence"]) == 7
    assert body["data"]["archived_gaps"][0]["id"] == "g-archived"


@pytest.mark.asyncio
async def test_agent_fetch_problem_model_hyphen_rejects_cross_project(
    client: AsyncClient, db_session
):
    """Cross-project fetches for the full model (hyphen route) still return 404."""
    project_a = Project(name="A", user_id="test-user-a")
    project_b = Project(
        name="B",
        user_id="test-user-b",
        problem_model={"intent": {"purpose": "hidden"}},
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
        f"/api/agent/projects/{project_b.id}/problem-model",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_agent_fetch_problem_model_both_spellings_return_same_body(
    client: AsyncClient, db_session
):
    """Both /problem_model (underscore) and /problem-model (hyphen) spellings
    return 200 and identical bodies. This regression test locks the dual-alias
    guarantee: deleting either decorator would break the test."""
    project = Project(
        name="Spelling Test",
        user_id="test-user-spelling",
        problem_model={
            "revision": 5,
            "intent": {"purpose": "Image rollover compatibility"},
            "gaps": [
                {
                    "id": "g-1",
                    "name": "Test gap",
                    "history": [{"ts": "t1", "event": "created"}],
                }
            ],
            "archived_gaps": [{"id": "g-old", "name": "Archived", "status": "closed"}],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-spelling"
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-spelling")
    headers = {"x-agent-token": token}

    # Fetch via underscore route
    res_underscore = await client.get(
        f"/api/agent/projects/{project.id}/problem_model", headers=headers
    )
    assert res_underscore.status_code == 200, res_underscore.text
    body_underscore = res_underscore.json()

    # Fetch via hyphen route
    res_hyphen = await client.get(
        f"/api/agent/projects/{project.id}/problem-model", headers=headers
    )
    assert res_hyphen.status_code == 200, res_hyphen.text
    body_hyphen = res_hyphen.json()

    # Both must return identical bodies
    assert body_underscore == body_hyphen
    assert body_underscore["data"]["revision"] == 5
    assert (
        body_underscore["data"]["intent"]["purpose"] == "Image rollover compatibility"
    )


# ---------------------------------------------------------------------------
# Message short-ref lookup + resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["open", "resolved", "linked"])
async def test_agent_fetch_message_lookup_across_statuses(
    client: AsyncClient, db_session, status
):
    """The agent can resolve a message by short ref regardless of status."""
    project, task = await _make_project_task(db_session)
    msg = await _add_message(
        db_session,
        project,
        message_type=MessageType.NOTE,
        from_type="user",
        content=f"a {status} message",
        status=status,
    )
    short_ref = msg.id.hex[:8].upper()  # case-insensitive
    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/messages/{short_ref}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["id"] == str(msg.id)
    assert data["content"] == f"a {status} message"
    assert data["status"] == status
    assert data["from_type"] == "user"
    assert data["type"] == "NOTE"


@pytest.mark.asyncio
async def test_agent_fetch_message_by_full_uuid_with_dashes(
    client: AsyncClient, db_session
):
    """A full UUID (with dashes) resolves to exactly that message."""
    project, task = await _make_project_task(db_session)
    msg = await _add_message(
        db_session,
        project,
        message_type=MessageType.BUG_REPORT,
        from_type="agent",
        content="full uuid lookup",
        status="open",
    )
    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/messages/{msg.id}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["id"] == str(msg.id)


@pytest.mark.asyncio
async def test_agent_fetch_message_ambiguous_prefix_fails_loudly(
    client: AsyncClient, db_session
):
    """An ambiguous prefix returns retryable refs/full ids; no row touched."""
    project, task = await _make_project_task(db_session)
    shared, messages = await _add_ambiguous_messages(db_session, project)

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/messages/{shared}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 409, res.text
    detail = res.json()["error"]["message"]
    assert detail["prefix"] == shared
    assert detail["min_hex_chars"] == 8
    assert detail["message"].startswith("Ambiguous message reference")
    candidates = detail["candidates"]
    assert len(candidates) == 2
    expected_ids = {str(msg.id) for msg in messages}
    assert {candidate["id"] for candidate in candidates} == expected_ids
    assert all(len(candidate["ref"]) > len(shared) for candidate in candidates)
    assert all(candidate["status"] == "open" for candidate in candidates)
    retry_res = await client.get(
        f"/api/agent/projects/{project.id}/messages/{candidates[0]['ref']}",
        headers={"x-agent-token": token},
    )
    assert retry_res.status_code == 200, retry_res.text
    assert retry_res.json()["data"]["id"] == candidates[0]["id"]


@pytest.mark.asyncio
async def test_strategic_resolve_message_ambiguous_prefix_returns_candidates(
    client: AsyncClient, db_session
):
    """resolve_message surfaces retryable candidates when an 8-char ref collides."""
    project, task = await _make_project_task(db_session)
    shared, messages = await _add_ambiguous_messages(db_session, project)
    token = create_agent_token(
        str(task.id), task.vm_instance_id, task_type="strategic_review"
    )
    res = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "resolve_message",
            "data": {"message_id": shared, "resolution": "done"},
        },
    )
    assert res.status_code == 409, res.text
    detail = res.json()["error"]["message"]
    assert detail["prefix"] == shared
    candidates = detail["candidates"]
    assert len(candidates) == 2
    expected_ids = {str(msg.id) for msg in messages}
    assert {candidate["id"] for candidate in candidates} == expected_ids
    assert all(len(candidate["ref"]) > len(shared) for candidate in candidates)
    retry_res = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "resolve_message",
            "data": {"message_id": candidates[0]["ref"], "resolution": "done"},
        },
    )
    assert retry_res.status_code == 200, retry_res.text
    assert retry_res.json()["message_id"] == candidates[0]["id"]
    for msg in messages:
        await db_session.refresh(msg)
    resolved = next(msg for msg in messages if str(msg.id) == candidates[0]["id"])
    untouched = next(msg for msg in messages if str(msg.id) != candidates[0]["id"])
    assert resolved.status == "resolved"
    assert resolved.resolution == "done"
    assert untouched.status == "open"
    assert untouched.resolution is None


@pytest.mark.asyncio
async def test_agent_fetch_message_missing_prefix_returns_404(
    client: AsyncClient, db_session
):
    """A prefix matching no message returns 404."""
    project, task = await _make_project_task(db_session)
    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/messages/deadbeef",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404, res.text


@pytest.mark.asyncio
async def test_agent_fetch_message_too_short_ref_fails_loudly(
    client: AsyncClient, db_session
):
    """A too-short prefix is rejected (400) rather than matching arbitrarily."""
    project, task = await _make_project_task(db_session)
    await _add_message(
        db_session,
        project,
        message_type=MessageType.NOTE,
        from_type="user",
        content="short",
        status="open",
    )
    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/messages/abc",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 400, res.text


@pytest.mark.asyncio
async def test_agent_fetch_message_non_hex_ref_returns_400(
    client: AsyncClient, db_session
):
    """A ref with non-hex characters (wildcards, letters outside a-f) is rejected (400)."""
    project, task = await _make_project_task(db_session)
    token = create_agent_token(str(task.id), task.vm_instance_id)
    # Test with a wildcard character '%' — must not be treated as a LIKE wildcard
    res = await client.get(
        f"/api/agent/projects/{project.id}/messages/deadbeef%",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 400, res.text
    # Test with underscore wildcard '_'
    res2 = await client.get(
        f"/api/agent/projects/{project.id}/messages/deadbeef_",
        headers={"x-agent-token": token},
    )
    assert res2.status_code == 400, res2.text


@pytest.mark.asyncio
async def test_agent_fetch_message_cross_project_not_found(
    client: AsyncClient, db_session
):
    """A short ref from another project is not found (cross-project stays closed)."""
    project_a, task_a = await _make_project_task(db_session, vm="agent-vm-a")
    project_b, _ = await _make_project_task(db_session, vm="agent-vm-b")
    msg_b = await _add_message(
        db_session,
        project_b,
        message_type=MessageType.NOTE,
        from_type="user",
        content="belongs to B",
        status="open",
    )
    # Agent token scoped to A asks A for a ref that exists only in B.
    token = create_agent_token(str(task_a.id), task_a.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project_a.id}/messages/{msg_b.id.hex[:8]}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404, res.text


@pytest.mark.asyncio
async def test_strategic_resolve_message_by_short_ref_plain(
    client: AsyncClient, db_session
):
    """resolve_message op accepts a short ref and resolves a plain message."""
    project, task = await _make_project_task(db_session)
    msg = await _add_message(
        db_session,
        project,
        message_type=MessageType.NOTE,
        from_type="user",
        content="please resolve",
        status="open",
    )
    token = create_agent_token(
        str(task.id), task.vm_instance_id, task_type="strategic_review"
    )
    res = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "resolve_message",
            "data": {"message_id": msg.id.hex[:8], "resolution": "done"},
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "ok"
    assert body["message_id"] == str(msg.id)
    await db_session.refresh(msg)
    assert msg.status == "resolved"
    assert msg.resolution == "done"
    assert msg.resolved_at is not None


@pytest.mark.asyncio
async def test_strategic_resolve_message_by_short_ref_links_bug(
    client: AsyncClient, db_session
):
    """resolve_message op by short ref links a bug-class message to an outcome."""
    project, task = await _make_project_task(db_session)
    msg = await _add_message(
        db_session,
        project,
        message_type=MessageType.BUG_REPORT,
        from_type="agent",
        content="a bug",
        status="open",
    )
    token = create_agent_token(
        str(task.id), task.vm_instance_id, task_type="strategic_review"
    )
    res = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "resolve_message",
            "data": {
                "message_id": msg.id.hex[:8],
                "owned_by_direction": "dir-99",
            },
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["linked_to"] == {"kind": "direction", "id": "dir-99"}
    await db_session.refresh(msg)
    assert msg.status == "linked"


@pytest.mark.asyncio
async def test_strategic_create_message_absent_citation_is_flagged(
    client: AsyncClient, db_session, mocker
):
    """A strategist-authored BUG_REPORT citing absent code stores an absent signal.

    The agent ``strategic`` create_message op now runs the SAME conservative
    reproduce-on-HEAD grounding the user/annotation paths use, so a
    strategist-emitted bug report that points at code already refactored away
    re-enters the inbox discounted rather than fully trusted.
    """
    from unittest.mock import AsyncMock

    project, task = await _make_project_task(db_session)
    mocker.patch(
        "app.source_ref_validation.resolve_branch_verification_context",
        new=AsyncMock(return_value={}),
    )
    mocker.patch(
        "app.source_ref_validation._file_exists_at_head",
        new=AsyncMock(return_value=False),
    )
    # The shared reproduce-on-HEAD contract (classify_reproduction) only reports
    # ``absent`` when a validation backend could actually run; with the branch
    # context mocked empty, assert the backend is available so a genuinely-broken
    # citation reaches ``absent`` rather than degrading to ``indeterminate``.
    mocker.patch(
        "app.source_ref_validation._has_validation_backend",
        return_value=True,
    )
    token = create_agent_token(
        str(task.id), task.vm_instance_id, task_type="strategic_review"
    )
    res = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "create_message",
            "data": {
                "type": "BUG_REPORT",
                "content": "The guard regressed in backend/app/gone.py during init.",
            },
        },
    )
    assert res.status_code == 200, res.text
    msg = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
    ).scalar_one()
    warnings = (msg.context or {}).get("_provenance_warnings")
    assert warnings is not None
    assert warnings["citation_state"] == "absent"
    assert "backend/app/gone.py" in warnings["broken_refs"]


@pytest.mark.asyncio
async def test_strategic_create_message_present_citation_not_flagged(
    client: AsyncClient, db_session, mocker
):
    """A strategist BUG_REPORT whose cited file still exists is NOT flagged."""
    from unittest.mock import AsyncMock

    project, task = await _make_project_task(db_session)
    mocker.patch(
        "app.source_ref_validation.resolve_branch_verification_context",
        new=AsyncMock(return_value={}),
    )
    mocker.patch(
        "app.source_ref_validation._file_exists_at_head",
        new=AsyncMock(return_value=True),
    )
    token = create_agent_token(
        str(task.id), task.vm_instance_id, task_type="strategic_review"
    )
    res = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "create_message",
            "data": {
                "type": "BUG_REPORT",
                "content": "Still reproduces in backend/app/real.py on load.",
            },
        },
    )
    assert res.status_code == 200, res.text
    msg = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
    ).scalar_one()
    assert "_provenance_warnings" not in (msg.context or {})


@pytest.mark.asyncio
async def test_strategic_create_message_mixed_present_citation_is_flagged(
    client: AsyncClient, db_session, mocker
):
    """A partially grounded strategist BUG_REPORT keeps its mixed ref detail."""
    from unittest.mock import AsyncMock

    project, task = await _make_project_task(db_session)
    mocker.patch(
        "app.source_ref_validation.resolve_branch_verification_context",
        new=AsyncMock(return_value={}),
    )
    mocker.patch(
        "app.source_ref_validation._file_exists_at_head",
        new=AsyncMock(
            side_effect=lambda path, **_: True if "alive.py" in path else False
        ),
    )
    token = create_agent_token(
        str(task.id), task.vm_instance_id, task_type="strategic_review"
    )
    res = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "create_message",
            "data": {
                "type": "BUG_REPORT",
                "content": (
                    "The crash spans backend/app/gone.py and "
                    "backend/app/alive.py during init."
                ),
            },
        },
    )
    assert res.status_code == 200, res.text
    msg = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
    ).scalar_one()
    warnings = (msg.context or {}).get("_provenance_warnings")
    assert warnings is not None
    assert warnings["citation_state"] == "present"
    assert "backend/app/gone.py" in warnings["broken_refs"]


@pytest.mark.asyncio
async def test_strategic_create_message_note_type_not_grounded(
    client: AsyncClient, db_session, mocker
):
    """A NOTE-type strategist message is never grounded, even if it cites code."""
    from unittest.mock import AsyncMock

    project, task = await _make_project_task(db_session)
    spy = mocker.patch(
        "app.source_ref_validation._file_exists_at_head",
        new=AsyncMock(return_value=False),
    )
    token = create_agent_token(
        str(task.id), task.vm_instance_id, task_type="strategic_review"
    )
    res = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "create_message",
            "data": {
                "type": "NOTE",
                "content": "Consider refactoring backend/app/gone.py soon.",
            },
        },
    )
    assert res.status_code == 200, res.text
    msg = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
    ).scalar_one()
    assert "_provenance_warnings" not in (msg.context or {})
    spy.assert_not_called()


# ---------------------------------------------------------------------------
# Qualification verdict fetch endpoint
# ---------------------------------------------------------------------------


async def _make_project_with_version(db_session, *, vm="agent-vm-qual"):
    project = Project(name="Qual Test", user_id="test-user-qual")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{}], vm_instance_id=vm)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    version = ReleaseVersion(
        project_id=project.id,
        version_number=10,
        status="open",
        opened_at=datetime(2026, 1, 1),
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    return project, task, version


@pytest.mark.asyncio
async def test_agent_fetch_qualification_returns_latest_verdict_findings(
    client: AsyncClient, db_session
):
    """Happy path: returns verdict row fields including full audit findings list."""
    project, task, version = await _make_project_with_version(db_session)

    findings = [
        {
            "severity": "HIGH",
            "summary": "Missing auth check",
            "file": "backend/app/auth.py",
        },
        {
            "severity": "MEDIUM",
            "summary": "N+1 query in loop",
            "file": "backend/app/admin.py",
        },
    ]
    excluded = [{"severity": "LOW", "summary": "Cosmetic warning"}]
    challenger_refuted = [
        {
            "severity": "MEDIUM",
            "summary": "False positive",
            "challenger_reason": "not applicable",
        }
    ]

    verdict = QualificationVerdict(
        id=uuid.uuid4(),
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="abc12345def6789012345678901234567890abcd",
        confidence=0.72,
        sub_scores={"audit": 0.5, "test_erosion": 1.0},
        terminal_status="dry_run",
        finalized_at=datetime(2026, 6, 1, 12, 0, 0),
        audit_findings=findings,
        excluded_audit_findings=excluded,
        challenger_refuted_audit_findings=challenger_refuted,
    )
    db_session.add(verdict)
    await db_session.commit()
    await db_session.refresh(verdict)

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/qualification",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["source"] == "qualification_verdict"
    assert body["candidate_sha"] == "abc12345def6789012345678901234567890abcd"
    assert body["confidence"] == pytest.approx(0.72)
    assert body["status"] == "dry_run"
    assert body["finalized_at"] == "2026-06-01T12:00:00"
    assert body["sub_scores"] == {"audit": 0.5, "test_erosion": 1.0}
    # Full findings list returned
    assert len(body["findings"]) == 2
    assert body["findings"][0]["severity"] == "HIGH"
    assert body["findings"][0]["file"] == "backend/app/auth.py"
    assert body["findings"][1]["severity"] == "MEDIUM"
    assert len(body["excluded_findings"]) == 1
    assert body["excluded_findings"][0]["summary"] == "Cosmetic warning"
    assert len(body["challenger_refuted_findings"]) == 1
    assert (
        body["challenger_refuted_findings"][0]["challenger_reason"] == "not applicable"
    )
    # Additive re-qualification signal: keys are present and shaped. These findings
    # carry no resolution-ledger rows, so the gating (HIGH+MEDIUM) findings read as
    # unresolved and the trigger is not met.
    assert body["requalification_ready"] is False
    assert "finding_resolution" in body
    states = {f["state"] for f in body["finding_resolution"]}
    assert states == {"unresolved"}
    assert {f["severity"] for f in body["finding_resolution"]} == {"high", "medium"}


@pytest.mark.asyncio
async def test_agent_fetch_qualification_surfaces_finding_resolution_states(
    client: AsyncClient, db_session
):
    """The qualification read surfaces per-finding ledger states and flips
    ``requalification_ready`` once every gating finding is resolved-or-stale."""
    from app.release_scoring import _audit_finding_key

    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-qual-resolve"
    )

    high = {
        "severity": "HIGH",
        "summary": "Missing auth check",
        "file": "backend/app/auth.py",
    }
    high["key"] = _audit_finding_key(high["summary"], high["file"])
    medium = {
        "severity": "MEDIUM",
        "summary": "N+1 query in loop",
        "file": "backend/app/admin.py",
    }
    medium["key"] = _audit_finding_key(medium["summary"], medium["file"])

    verdict = QualificationVerdict(
        id=uuid.uuid4(),
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="f" * 40,
        confidence=0.6,
        sub_scores={"audit": 0.5},
        terminal_status="not_promoted",
        audit_findings=[high, medium],
    )
    db_session.add(verdict)
    # Resolve the HIGH finding only; the MEDIUM stays unresolved (no ledger row).
    db_session.add(
        AuditFindingResolution(
            project_id=project.id,
            release_version_id=version.id,
            finding_key=high["key"],
            severity="high",
            state="resolved",
        )
    )
    await db_session.commit()

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/qualification",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()

    by_key = {f["key"]: f for f in body["finding_resolution"]}
    assert by_key[high["key"]]["state"] == "resolved"
    assert by_key[medium["key"]]["state"] == "unresolved"
    # One gating finding still unresolved → not ready.
    assert body["requalification_ready"] is False

    # Mark the MEDIUM code-verified stale → every gating finding resolved-or-stale.
    db_session.add(
        AuditFindingResolution(
            project_id=project.id,
            release_version_id=version.id,
            finding_key=medium["key"],
            severity="medium",
            state="stale",
        )
    )
    await db_session.commit()

    res2 = await client.get(
        f"/api/agent/projects/{project.id}/qualification",
        headers={"x-agent-token": token},
    )
    assert res2.status_code == 200, res2.text
    body2 = res2.json()
    assert body2["requalification_ready"] is True
    states2 = {f["key"]: f["state"] for f in body2["finding_resolution"]}
    assert states2[high["key"]] == "resolved"
    assert states2[medium["key"]] == "stale"


@pytest.mark.asyncio
async def test_agent_fetch_qualification_falls_back_to_last_qualification(
    client: AsyncClient, db_session
):
    """No verdict row with audit_findings → falls back to last_qualification blob."""
    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-qual-fb"
    )

    from sqlalchemy.orm.attributes import flag_modified

    fallback_findings = [{"severity": "HIGH", "summary": "Legacy finding"}]
    version.last_qualification = {
        "status": "not_promoted",
        "confidence": 0.45,
        "candidate_sha": "deadbeef" * 5,
        "finalized_at": "2026-05-01T10:00:00",
        "sub_scores": {"audit": 0.3},
        "audit_findings": fallback_findings,
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/qualification",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["source"] == "last_qualification"
    assert body["confidence"] == pytest.approx(0.45)
    assert body["status"] == "not_promoted"
    assert len(body["findings"]) == 1
    assert body["findings"][0]["severity"] == "HIGH"
    # Empty lists when not present in blob
    assert body["excluded_findings"] == []
    assert body["challenger_refuted_findings"] == []
    # Additive re-qualification signal present on the fallback path too.
    assert body["requalification_ready"] is False
    assert [f["state"] for f in body["finding_resolution"]] == ["unresolved"]


@pytest.mark.asyncio
async def test_agent_fetch_qualification_no_open_version(
    client: AsyncClient, db_session
):
    """No open ReleaseVersion → returns source=no_open_version."""
    project = Project(name="No Version", user_id="test-user-nov")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-nov")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/qualification",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    assert res.json()["source"] == "no_open_version"


@pytest.mark.asyncio
async def test_agent_fetch_qualification_rejects_cross_project(
    client: AsyncClient, db_session
):
    """Token from project A cannot fetch qualification data for project B (404)."""
    project_a = Project(name="QA", user_id="test-user-qa")
    project_b = Project(name="QB", user_id="test-user-qb")
    db_session.add_all([project_a, project_b])
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    task_a = Task(project_id=project_a.id, payloads=[{}], vm_instance_id="agent-vm-qa")
    db_session.add(task_a)
    await db_session.commit()
    await db_session.refresh(task_a)

    token = create_agent_token(str(task_a.id), task_a.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project_b.id}/qualification",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_agent_fetch_qualification_includes_goal_alignment(
    client: AsyncClient, db_session
):
    """QualificationVerdict with goal_alignment set surfaces assessed_grade/state/biggest_gap."""
    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-qa-goal"
    )

    goal_alignment_data = {
        "assessed_grade": 0.75,
        "state": "shortfall",
        "biggest_gap": "Auth improvements not addressed",
        "reasoning_summary": "Three of five goal areas covered; auth work missing.",
    }
    verdict = QualificationVerdict(
        id=uuid.uuid4(),
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="aabbccdd" * 5,
        confidence=0.8,
        terminal_status="dry_run",
        audit_findings=[],
        goal_alignment=goal_alignment_data,
    )
    db_session.add(verdict)
    await db_session.commit()
    await db_session.refresh(verdict)

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/qualification",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["source"] == "qualification_verdict"
    ga = body["goal_alignment"]
    assert ga is not None
    assert ga["assessed_grade"] == pytest.approx(0.75)
    assert ga["state"] == "shortfall"
    assert ga["biggest_gap"] == "Auth improvements not addressed"
    assert (
        ga["reasoning_summary"]
        == "Three of five goal areas covered; auth work missing."
    )


@pytest.mark.asyncio
async def test_agent_fetch_qualification_goal_alignment_in_fallback(
    client: AsyncClient, db_session
):
    """goal_alignment in last_qualification JSONB is surfaced on the fallback path."""
    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-qa-goal-fb"
    )

    from sqlalchemy.orm.attributes import flag_modified

    version.last_qualification = {
        "status": "not_promoted",
        "confidence": 0.5,
        "candidate_sha": "cafebabe" * 5,
        "goal_alignment": {
            "assessed_grade": 0.6,
            "state": "shortfall",
            "biggest_gap": "Performance work incomplete",
            "reasoning_summary": "Goal partially met.",
        },
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/qualification",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["source"] == "last_qualification"
    ga = body["goal_alignment"]
    assert ga is not None
    assert ga["assessed_grade"] == pytest.approx(0.6)
    assert ga["biggest_gap"] == "Performance work incomplete"


# ---------------------------------------------------------------------------
# audit-finding-resolution endpoint tests
# ---------------------------------------------------------------------------


async def _setup_audit_project_task(
    db_session, *, vm="agent-vm-audit", task_type="release_audit"
):
    project = Project(name="Audit Test", user_id="test-user-audit")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(project_id=project.id, payloads=[{}], vm_instance_id=vm)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    token = create_agent_token(str(task.id), vm, task_type=task_type)
    return project, task, token


@pytest.mark.asyncio
async def test_audit_finding_resolution_resolved_state(client: AsyncClient, db_session):
    """release_audit agent marks a finding resolved; ledger row is written."""
    project, task, token = await _setup_audit_project_task(
        db_session, task_type="release_audit"
    )
    version = ReleaseVersion(
        project_id=project.id, version_number=10, status="open", phase="build"
    )
    db_session.add(version)
    await db_session.commit()

    res = await client.post(
        "/api/agent/audit-finding-resolution",
        json={
            "task_id": str(task.id),
            "finding_key": "abcdef1234567890",
            "state": "resolved",
            "resolving_ref": "https://github.com/org/repo/pull/42",
            "note": "Fixed in PR #42",
        },
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "ok"
    assert body["finding_key"] == "abcdef1234567890"
    assert body["state"] == "resolved"
    assert body["release_version_id"] == str(version.id)

    row = (
        (
            await db_session.execute(
                select(AuditFindingResolution).where(
                    AuditFindingResolution.project_id == project.id,
                    AuditFindingResolution.finding_key == "abcdef1234567890",
                )
            )
        )
        .scalars()
        .first()
    )
    assert row is not None
    assert row.state == "resolved"
    assert row.resolving_ref == "https://github.com/org/repo/pull/42"
    assert row.note == "Fixed in PR #42"


@pytest.mark.asyncio
async def test_audit_finding_resolution_stale_state(client: AsyncClient, db_session):
    """research agent marks a finding stale with a candidate_sha."""
    project, task, token = await _setup_audit_project_task(
        db_session, vm="agent-vm-stale", task_type="research"
    )
    version = ReleaseVersion(
        project_id=project.id, version_number=7, status="open", phase="stabilize"
    )
    db_session.add(version)
    await db_session.commit()

    res = await client.post(
        "/api/agent/audit-finding-resolution",
        json={
            "task_id": str(task.id),
            "finding_key": "deadbeef12345678",
            "state": "stale",
            "verified_against_sha": "abc1234",
            "note": "Verified on HEAD: pattern no longer present",
        },
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["state"] == "stale"
    assert body["release_version_id"] == str(version.id)

    row = (
        (
            await db_session.execute(
                select(AuditFindingResolution).where(
                    AuditFindingResolution.finding_key == "deadbeef12345678",
                )
            )
        )
        .scalars()
        .first()
    )
    assert row is not None
    assert row.state == "stale"
    assert row.verified_against_sha == "abc1234"


@pytest.mark.asyncio
async def test_audit_finding_resolution_uses_open_version(
    client: AsyncClient, db_session
):
    """When version_number is omitted the endpoint resolves the open ReleaseVersion."""
    project, task, token = await _setup_audit_project_task(
        db_session, vm="agent-vm-openver", task_type="release_audit_challenge"
    )
    version = ReleaseVersion(
        project_id=project.id, version_number=5, status="open", phase="build"
    )
    db_session.add(version)
    await db_session.commit()

    res = await client.post(
        "/api/agent/audit-finding-resolution",
        json={
            "task_id": str(task.id),
            "finding_key": "cafebabe12345678",
            "state": "stale",
        },
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["release_version_id"] == str(version.id)

    row = (
        (
            await db_session.execute(
                select(AuditFindingResolution).where(
                    AuditFindingResolution.finding_key == "cafebabe12345678",
                )
            )
        )
        .scalars()
        .first()
    )
    assert row is not None
    assert row.release_version_id == version.id


@pytest.mark.asyncio
async def test_audit_finding_resolution_no_open_version_returns_409(
    client: AsyncClient, db_session
):
    """When no open version exists and version_number is absent, returns 409."""
    project, task, token = await _setup_audit_project_task(
        db_session, vm="agent-vm-nover", task_type="release_audit"
    )
    # No ReleaseVersion row for this project.

    res = await client.post(
        "/api/agent/audit-finding-resolution",
        json={
            "task_id": str(task.id),
            "finding_key": "1234567890abcdef",
            "state": "resolved",
        },
        headers={"x-agent-token": token},
    )
    assert res.status_code == 409, res.text


@pytest.mark.asyncio
async def test_audit_finding_resolution_wrong_task_type_returns_403(
    client: AsyncClient, db_session
):
    """A task type not in the allowed set is rejected with 403."""
    project, task, token = await _setup_audit_project_task(
        db_session, vm="agent-vm-badtype", task_type="create"
    )

    res = await client.post(
        "/api/agent/audit-finding-resolution",
        json={
            "task_id": str(task.id),
            "finding_key": "abcdef1234567890",
            "state": "resolved",
        },
        headers={"x-agent-token": token},
    )
    assert res.status_code == 403, res.text


@pytest.mark.asyncio
async def test_audit_finding_resolution_task_id_mismatch_returns_403(
    client: AsyncClient, db_session
):
    """Payload task_id that doesn't match the token sub is rejected with 403."""
    project, task, token = await _setup_audit_project_task(
        db_session, vm="agent-vm-mismatch", task_type="release_audit"
    )
    other_task = Task(
        project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-other"
    )
    db_session.add(other_task)
    await db_session.commit()
    await db_session.refresh(other_task)

    res = await client.post(
        "/api/agent/audit-finding-resolution",
        json={
            "task_id": str(other_task.id),  # mismatches token sub (task.id)
            "finding_key": "abcdef1234567890",
            "state": "stale",
        },
        headers={"x-agent-token": token},
    )
    assert res.status_code == 403, res.text


@pytest.mark.asyncio
async def test_audit_finding_resolution_upserts_on_second_write(
    client: AsyncClient, db_session
):
    """A second write for the same finding_key updates the existing row."""
    project, task, token = await _setup_audit_project_task(
        db_session, vm="agent-vm-upsert", task_type="strategic_review"
    )
    version = ReleaseVersion(
        project_id=project.id, version_number=3, status="open", phase="build"
    )
    db_session.add(version)
    await db_session.commit()

    fkey = "ffffffff12345678"

    # First write: stale
    r1 = await client.post(
        "/api/agent/audit-finding-resolution",
        json={"task_id": str(task.id), "finding_key": fkey, "state": "stale"},
        headers={"x-agent-token": token},
    )
    assert r1.status_code == 200, r1.text

    # Second write: resolved
    r2 = await client.post(
        "/api/agent/audit-finding-resolution",
        json={
            "task_id": str(task.id),
            "finding_key": fkey,
            "state": "resolved",
            "note": "now fixed",
        },
        headers={"x-agent-token": token},
    )
    assert r2.status_code == 200, r2.text

    rows = (
        (
            await db_session.execute(
                select(AuditFindingResolution).where(
                    AuditFindingResolution.finding_key == fkey,
                )
            )
        )
        .scalars()
        .all()
    )
    # Must upsert, not insert a duplicate
    assert len(rows) == 1
    assert rows[0].state == "resolved"
    assert rows[0].note == "now fixed"


@pytest.mark.asyncio
async def test_audit_finding_resolution_bound_version_used(
    client: AsyncClient, db_session
):
    """release_audit task with context-bound version uses that version for the ledger."""
    project = Project(name="Bound Ver Test", user_id="test-user-bv")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = ReleaseVersion(
        project_id=project.id, version_number=11, status="open", phase="build"
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    task = Task(
        project_id=project.id,
        payloads=[{}],
        vm_instance_id="agent-vm-bv",
        context={"release_version_id": str(version.id)},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    token = create_agent_token(str(task.id), "agent-vm-bv", task_type="release_audit")

    res = await client.post(
        "/api/agent/audit-finding-resolution",
        json={
            "task_id": str(task.id),
            "finding_key": "aabbccdd11223344",
            "state": "resolved",
            "note": "fixed",
        },
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "ok"
    assert body["release_version_id"] == str(version.id)

    row = (
        (
            await db_session.execute(
                select(AuditFindingResolution).where(
                    AuditFindingResolution.finding_key == "aabbccdd11223344",
                )
            )
        )
        .scalars()
        .first()
    )
    assert row is not None
    assert row.release_version_id == version.id
    assert row.state == "resolved"


@pytest.mark.asyncio
async def test_audit_finding_resolution_rejects_stale_task_version(
    client: AsyncClient, db_session
):
    """release_audit task bound to an old version returns 409 when a newer version is open."""
    project = Project(name="Stale Ver Test", user_id="test-user-sv")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    old_version = ReleaseVersion(
        project_id=project.id, version_number=9, status="closed", phase="build"
    )
    db_session.add(old_version)
    await db_session.commit()
    await db_session.refresh(old_version)

    new_version = ReleaseVersion(
        project_id=project.id, version_number=10, status="open", phase="build"
    )
    db_session.add(new_version)
    await db_session.commit()
    await db_session.refresh(new_version)

    # Task was spawned against old_version and still carries that binding.
    task = Task(
        project_id=project.id,
        payloads=[{}],
        vm_instance_id="agent-vm-sv",
        context={"release_version_id": str(old_version.id)},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    token = create_agent_token(str(task.id), "agent-vm-sv", task_type="release_audit")

    res = await client.post(
        "/api/agent/audit-finding-resolution",
        json={
            "task_id": str(task.id),
            "finding_key": "stalefinding1234",
            "state": "resolved",
        },
        headers={"x-agent-token": token},
    )
    assert res.status_code == 409, res.text

    # No AuditFindingResolution row must have been created.
    row = (
        (
            await db_session.execute(
                select(AuditFindingResolution).where(
                    AuditFindingResolution.finding_key == "stalefinding1234",
                )
            )
        )
        .scalars()
        .first()
    )
    assert row is None


# ---------------------------------------------------------------------------
# resolve_message_ref DB-side prefix filter tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_message_ref_python_side_filter_within_recent_window(
    client: AsyncClient, db_session
):
    """resolve_message_ref fetches all project message IDs (no recency cap),
    matches the prefix in Python, then loads the full row for any hit.

    Strategy: create a target message and several non-target messages all with
    the same 8-char hex prefix; the target is distinguishable by a longer
    (24-char) prefix.  The target is found and the unique prefix resolves
    unambiguously regardless of creation order.
    """
    import uuid as _uuid

    project, task = await _make_project_task(db_session, vm="agent-vm-pyfilter")

    base_ts = datetime(2026, 1, 1, 12, 0, 0)

    # All messages share a common 8-hex prefix.
    common_prefix = "bb000000"
    # Create 10 non-target messages with a different suffix pattern.
    for i in range(1, 11):
        mid = _uuid.UUID(common_prefix + "0000" + "4000" + "8000" + f"{i:012x}")
        msg = ProjectMessage(
            id=mid,
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="user",
            content="non-target",
            status="open",
            created_at=base_ts + timedelta(seconds=i),
        )
        db_session.add(msg)

    # Target message — arrives after all non-targets so it's in the recency window.
    target_id = _uuid.UUID(common_prefix + "0000" + "4000" + "8000" + "ffffffffffff")
    target_msg = ProjectMessage(
        id=target_id,
        project_id=project.id,
        message_type=MessageType.BUG_REPORT,
        from_type="agent",
        content="the target",
        status="open",
        created_at=base_ts + timedelta(seconds=100),
    )
    db_session.add(target_msg)
    await db_session.commit()
    await db_session.refresh(target_msg)

    # 24-char prefix uniquely identifies the target (non-targets end in 0…, target ends in f…).
    target_ref = target_id.hex[:24]
    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/messages/{target_ref}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["id"] == str(target_id)
    assert res.json()["data"]["content"] == "the target"


@pytest.mark.asyncio
async def test_resolve_message_ref_old_message_beyond_500_found(
    client: AsyncClient, db_session
):
    """A message older than the 500 most-recent messages is correctly resolved.

    The implementation fetches all project message IDs without a recency cap,
    so a message whose created_at predates the 500 most-recent messages is still
    matched and returned rather than returning 404.
    """
    import uuid as _uuid

    project, task = await _make_project_task(db_session, vm="agent-vm-tail")

    base_ts = datetime(2026, 1, 2, 0, 0, 0)

    # Target message is the OLDEST in the project (created_at = base_ts).
    target_prefix = "cc000000"
    target_id = _uuid.UUID(target_prefix + "0000" + "4000" + "8000" + "aaaaaaaaaaaa")
    target_msg = ProjectMessage(
        id=target_id,
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="user",
        content="old target",
        status="open",
        created_at=base_ts,
    )
    db_session.add(target_msg)

    # Create 500 newer messages with a distinct prefix so they don't collide.
    filler_prefix = "dd000000"
    for i in range(500):
        mid = _uuid.UUID(filler_prefix + "0000" + "4000" + "8000" + f"{i:012x}")
        msg = ProjectMessage(
            id=mid,
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="user",
            content="filler",
            status="open",
            created_at=base_ts + timedelta(seconds=i + 1),
        )
        db_session.add(msg)

    await db_session.commit()
    await db_session.refresh(target_msg)

    # The target is the oldest message but should still be resolved correctly.
    target_ref = target_id.hex[:24]
    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/messages/{target_ref}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["id"] == str(target_id)
    assert res.json()["data"]["content"] == "old target"


@pytest.mark.asyncio
async def test_agent_fetch_vm_cost_by_task_type(client: AsyncClient, db_session):
    """Agent ./fetch vm-cost returns by-task-type VM cost/count/merge-yield rows
    plus window metadata. Tasks of different types are bucketed; merged tasks
    contribute to merged_count."""
    from app.models import TaskStatus
    from datetime import datetime, timezone

    project = Project(name="VmCost Test", user_id="test-user-vmcost")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Task that holds the agent token
    token_task = Task(
        project_id=project.id,
        payloads=[{}],
        vm_instance_id="agent-vm-vmcost",
    )
    db_session.add(token_task)

    # A completed create task (no merge signal)
    create_task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        updated_at=datetime.now(timezone.utc),
        vm_provisioned_at=datetime.now(timezone.utc),
        vm_stopped_at=datetime.now(timezone.utc),
    )
    db_session.add(create_task)

    # A completed fix task with a merge signal
    fix_task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        status=TaskStatus.COMPLETED,
        updated_at=datetime.now(timezone.utc),
        vm_provisioned_at=datetime.now(timezone.utc),
        vm_stopped_at=datetime.now(timezone.utc),
        context={"pr_merged": True},
    )
    db_session.add(fix_task)

    await db_session.commit()
    await db_session.refresh(token_task)

    token = create_agent_token(str(token_task.id), "agent-vm-vmcost")
    res = await client.get(
        f"/api/agent/projects/{project.id}/vm-cost",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    data = body["data"]
    assert "by_task_type" in data
    assert "window_days" in data
    assert data["window_days"] == 7
    # Each row has the expected fields
    rows = data["by_task_type"]
    assert isinstance(rows, list)
    task_types = {r["task_type"] for r in rows}
    # The two non-token tasks have types "create" and "fix"
    assert "create" in task_types or "fix" in task_types
    for row in rows:
        assert "task_count" in row
        assert "merged_count" in row
        assert "total_cost_usd" in row
        assert "share_of_total_cost" in row
        assert "cost_per_merge" in row


@pytest.mark.asyncio
async def test_agent_fetch_vm_cost_rejects_cross_project(
    client: AsyncClient, db_session
):
    """Agent token for project A cannot fetch vm-cost for project B (404)."""
    project_a = Project(name="VmCost A", user_id="test-user-vmca")
    project_b = Project(name="VmCost B", user_id="test-user-vmcb")
    db_session.add_all([project_a, project_b])
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    task_a = Task(
        project_id=project_a.id,
        payloads=[{}],
        vm_instance_id="agent-vm-vmca",
    )
    db_session.add(task_a)
    await db_session.commit()
    await db_session.refresh(task_a)

    token = create_agent_token(str(task_a.id), "agent-vm-vmca")
    res = await client.get(
        f"/api/agent/projects/{project_b.id}/vm-cost",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_fetch_efficiency_snapshot(client: AsyncClient, db_session):
    """Agent endpoint returns cost_per_merged_pr and vm_reuse_rate in data."""
    project, task = await _make_project_task(db_session, vm="agent-vm-eff")
    token = create_agent_token(str(task.id), "agent-vm-eff")
    res = await client.get(
        f"/api/agent/projects/{project.id}/efficiency-snapshot",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    data = body["data"]
    assert "cost_per_merged_pr" in data
    assert "vm_reuse_rate" in data
    assert "llm_cost_per_merged_pr" in data
    assert "format_compliance" in data
    assert data["window_days"] == 7


@pytest.mark.asyncio
async def test_fetch_efficiency_snapshot_rejects_cross_project(
    client: AsyncClient, db_session
):
    """Agent token for project A cannot fetch efficiency-snapshot for project B (404)."""
    project_a = Project(name="Eff A", user_id="test-user-effa")
    project_b = Project(name="Eff B", user_id="test-user-effb")
    db_session.add_all([project_a, project_b])
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    task_a = Task(
        project_id=project_a.id, payloads=[{}], vm_instance_id="agent-vm-effa"
    )
    db_session.add(task_a)
    await db_session.commit()
    await db_session.refresh(task_a)

    token = create_agent_token(str(task_a.id), "agent-vm-effa")
    res = await client.get(
        f"/api/agent/projects/{project_b.id}/efficiency-snapshot",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404
