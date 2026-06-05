"""Tests for the agent strategic-context fetch sub-router
(app.routers.agent_strategic_fetch): read-only direction/playbook/gap
fetch endpoints, their auth/cross-project semantics, history capping,
pending_source visibility, and archive lineage.

Relocated from test_agent.py when the strategic fetch surface was extracted
into its own router seam; behavior and contracts are unchanged.
"""

import pytest
from httpx import AsyncClient

from app.models import MessageType, Project, ProjectMessage, Task, TaskStatus
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


async def _add_ambiguous_messages(db_session, project, *, shared_prefix: str = "abcdef12"):
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
    assert body_underscore["data"]["intent"]["purpose"] == "Image rollover compatibility"


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
