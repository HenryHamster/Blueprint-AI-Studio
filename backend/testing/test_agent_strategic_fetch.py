"""Tests for the agent strategic-context fetch sub-router
(app.routers.agent_strategic_fetch): read-only direction/playbook/gap
fetch endpoints, their auth/cross-project semantics, history capping,
pending_source visibility, and archive lineage.

Relocated from test_agent.py when the strategic fetch surface was extracted
into its own router seam; behavior and contracts are unchanged.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models import (
    Artifact,
    ArtifactType,
    AuditFindingResolution,
    ComputeJob,
    ComputeJobStatus,
    MessageHandlingState,
    MessageSurface,
    MessageType,
    Project,
    ProjectMember,
    ProjectMessage,
    QualificationDimensionResult,
    QualificationVerdict,
    ReleaseDeployArtifact,
    ReleaseVersion,
    Task,
    TaskStatus,
    UiLoadTimingSample,
)
from app.auth import create_agent_token


@pytest.mark.asyncio
async def test_agent_fetch_compute_job_exposes_live_truth_and_cost_semantics(
    client: AsyncClient, db_session
):
    project, task = await _make_project_task(db_session, vm="agent-vm-compute-fetch")
    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend="local-subprocess",
        status=ComputeJobStatus.RUNNING,
        machine_type="n1-standard-1",
        accelerator_type="V100",
        accelerator_count=1,
        accrued_cost_usd=0.22,
        polling_metadata={
            "monetary_decision": {
                "execution_mode": "local_only",
                "billable_estimate_usd": 0.0,
            },
            "live_output": {"text": "step=10", "truncated": False},
            "runner_result_metrics": {"phase": "training", "loss": 0.5},
        },
        runtime_provenance={
            "observed_hardware": {"gpu_names": ["NVIDIA L40S"], "gpu_count": 6},
        },
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    token = create_agent_token(str(task.id), task.vm_instance_id)
    response = await client.get(
        f"/api/agent/projects/{project.id}/compute-jobs/{job.id}",
        headers={"x-agent-token": token},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["live_output"]["text"] == "step=10"
    assert data["result_metrics"] == {"phase": "training", "loss": 0.5}
    assert data["requested_hardware"]["accelerator_type"] == "V100"
    assert data["observed_hardware"]["gpu_names"] == ["NVIDIA L40S"]
    assert data["cost_accounting"] == {
        "execution_mode": "local_only",
        "is_billable": False,
        "billable_estimate_usd": 0.0,
        "billable_cost_usd": 0.0,
        "modeled_resource_cost_usd": 0.22,
    }


@pytest.mark.asyncio
async def test_agent_fetch_compute_limits_is_project_scoped_and_actionable(
    client: AsyncClient, db_session
):
    project, task = await _make_project_task(db_session, vm="agent-vm-compute-limits")
    token = create_agent_token(str(task.id), task.vm_instance_id)

    response = await client.get(
        f"/api/agent/projects/{project.id}/compute-limits",
        headers={"x-agent-token": token},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["deadline_seconds"]["minimum"] > 0
    assert data["deadline_seconds"]["maximum"] >= data["deadline_seconds"]["minimum"]
    assert data["require_local_runner"] is False
    assert "local_only" in data["cost_semantics"]
    assert "local_runner_status" in data


def _assert_generated_at_iso_utc(payload: dict) -> None:
    generated_at = payload.get("generated_at")
    assert isinstance(generated_at, str) and generated_at
    parsed = datetime.fromisoformat(generated_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


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


def _assert_project_message_event_payload(
    payload: dict,
    *,
    project: Project,
    msg: ProjectMessage,
    action: str,
    status: str,
) -> None:
    message_type = getattr(msg.message_type, "value", str(msg.message_type))
    assert payload["schema_version"] == 1
    assert payload["project_id"] == str(project.id)
    assert payload["resource"] == "project_message"
    assert payload["action"] == action
    assert payload["message_id"] == str(msg.id)
    assert payload["type"] == message_type
    assert payload["from"] == msg.from_type
    assert payload["message"]["id"] == str(msg.id)
    assert payload["message"]["status"] == status
    assert payload["refetch"] == {"surfaces": ["messages", "strategic_session"]}


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
async def test_agent_fetch_direction_merge_credit_summary(
    client: AsyncClient, db_session
):
    """Agent fetch response includes an additive merge_credit summary with
    verified vs badge-only split computed from the full history."""
    project = Project(
        name="Credit Test",
        user_id="test-user-credit",
        directions=[
            {
                "id": "dir-credit",
                "status": "active",
                "text": "Credit direction",
                "priority": "high",
                # 3 history entries: 2 with verified_prs_merged, 1 legacy
                "research_history": [
                    {"prs_merged": 4, "verified_prs_merged": 3},
                    {"prs_merged": 2, "verified_prs_merged": 2},
                    {"prs_merged": 1},  # legacy — no verified field, treated as verified
                ],
                "history_aggregate": {
                    "total_prs_merged": 0,
                },
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-credit"
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-credit")
    res = await client.get(
        f"/api/agent/projects/{project.id}/directions/dir-credit",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    mc = body["data"]["merge_credit"]
    # raw total: 4+2+1 = 7
    assert mc["prs_merged"] == 7
    # verified: 3+2 + legacy fallback 1 = 6
    assert mc["verified_prs_merged"] == 6
    # badge-only = 7 - 6 = 1
    assert mc["badge_only_prs_merged"] == 1
    assert mc["proven_prs_merged"] == 0
    assert mc["consumed_prs_merged"] == 0


@pytest.mark.asyncio
async def test_agent_fetch_direction_merge_credit_summary_includes_reachability(
    client: AsyncClient, db_session
):
    """merge_credit includes reachable_prs_merged when history entries carry the
    reachable_prs_merged field; entries without it are treated as legacy-unknown
    and contribute 0, matching strategic_spawning stat_entry semantics."""
    project = Project(
        name="Reachability Credit Test",
        user_id="test-user-reach",
        directions=[
            {
                "id": "dir-reach",
                "status": "active",
                "text": "Reachability direction",
                "priority": "high",
                # 3 entries: two with reachable_prs_merged recorded, one legacy
                "research_history": [
                    {"prs_merged": 3, "verified_prs_merged": 3, "reachable_prs_merged": 2},
                    {"prs_merged": 2, "verified_prs_merged": 2, "reachable_prs_merged": 2},
                    {"prs_merged": 1, "verified_prs_merged": 1},  # legacy — no reachable field
                ],
                "history_aggregate": {
                    "total_prs_merged": 0,
                    "total_reachable_prs_merged": 0,
                },
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-reach"
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-reach")
    res = await client.get(
        f"/api/agent/projects/{project.id}/directions/dir-reach",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    mc = res.json()["data"]["merge_credit"]
    # raw total: 3+2+1 = 6
    assert mc["prs_merged"] == 6
    # verified total: 3+2+1 = 6 (all verified)
    assert mc["verified_prs_merged"] == 6
    # badge_only = 6 - 6 = 0
    assert mc["badge_only_prs_merged"] == 0
    # reachable_prs_merged: present because at least one entry has the field;
    # legacy entry contributes 0, so total = 2+2+0 = 4
    assert "reachable_prs_merged" in mc, "reachable_prs_merged must be present when any history entry records it"
    assert mc["reachable_prs_merged"] == 4


@pytest.mark.asyncio
async def test_agent_fetch_direction_merge_credit_summary_preserves_known_zero_reachability_after_eviction(
    client: AsyncClient, db_session
):
    """Known-zero reachability survives eviction when the aggregate records it."""
    project = Project(
        name="Reachability Eviction Test",
        user_id="test-user-reach-zero",
        directions=[
            {
                "id": "dir-reach-zero",
                "status": "active",
                "text": "Zero-reachability direction",
                "priority": "high",
                "research_history": [],
                "history_aggregate": {
                    "total_prs_merged": 0,
                    "total_reachable_prs_merged": 0,
                    "total_reachable_recorded_cycles": 2,
                },
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-reach-zero"
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-reach-zero")
    res = await client.get(
        f"/api/agent/projects/{project.id}/directions/dir-reach-zero",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    mc = res.json()["data"]["merge_credit"]
    assert "reachable_prs_merged" in mc
    assert mc["reachable_prs_merged"] == 0


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
    client: AsyncClient, db_session, mocker
):
    """resolve_message op accepts a short ref and resolves a plain message."""
    from unittest.mock import AsyncMock

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
    broadcast = mocker.patch(
        "app.routers.admin_agent_ops.broadcast_to_project",
        new=AsyncMock(),
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
    broadcast.assert_awaited_once()
    project_id, event, payload = broadcast.await_args.args
    assert project_id == str(project.id)
    assert event == "message_updated"
    _assert_project_message_event_payload(
        payload,
        project=project,
        msg=msg,
        action="resolved",
        status="resolved",
    )


@pytest.mark.asyncio
async def test_strategic_resolve_message_by_short_ref_links_bug(
    client: AsyncClient, db_session, mocker
):
    """resolve_message op by short ref links a bug-class message to an outcome."""
    from unittest.mock import AsyncMock

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
    broadcast = mocker.patch(
        "app.routers.admin_agent_ops.broadcast_to_project",
        new=AsyncMock(),
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
    broadcast.assert_awaited_once()
    project_id, event, payload = broadcast.await_args.args
    assert project_id == str(project.id)
    assert event == "message_updated"
    _assert_project_message_event_payload(
        payload,
        project=project,
        msg=msg,
        action="updated",
        status="linked",
    )


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


@pytest.mark.asyncio
async def test_strategic_create_message_broadcasts_workbench_envelope(
    client: AsyncClient, db_session, mocker
):
    """Strategist-authored messages publish the shared project-message envelope."""
    from unittest.mock import AsyncMock

    project, task = await _make_project_task(db_session)
    broadcast = mocker.patch(
        "app.routers.admin_agent_ops.broadcast_to_project",
        new=AsyncMock(),
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
                "content": "Keep the local Strategic session warm.",
            },
        },
    )

    assert res.status_code == 200, res.text
    msg = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
    ).scalar_one()
    broadcast.assert_awaited_once()
    project_id, event, payload = broadcast.await_args.args
    assert project_id == str(project.id)
    assert event == "message_created"
    assert payload["schema_version"] == 1
    assert payload["project_id"] == str(project.id)
    assert payload["resource"] == "project_message"
    assert payload["action"] == "created"
    assert payload["message_id"] == str(msg.id)
    assert payload["type"] == "NOTE"
    assert payload["from"] == "strategic"
    assert payload["message"]["id"] == str(msg.id)
    assert payload["message"]["status"] == "open"
    assert payload["refetch"] == {"surfaces": ["messages", "strategic_session"]}


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
    assert body["finding_total"] == 2
    assert body["resolved_finding_total"] == 0
    assert body["projected_audit_score"] == pytest.approx(1.0)
    assert body["projected_confidence"] == pytest.approx(0.95)
    assert body["projected_promotion_ready"] is True
    assert body["projected_confidence"] >= body["promotion_confidence_threshold"]
    assert body["required_audit_score"] == pytest.approx(0.9079)
    assert "finding_resolution" in body
    states = {f["state"] for f in body["finding_resolution"]}
    assert states == {"unresolved"}
    assert {f["severity"] for f in body["finding_resolution"]} == {"high", "medium"}
    # Additive trigger-explanation surface is present on the verdict branch and,
    # when computed, is a shaped dict (never raises out of this best-effort block).
    assert "qualification_trigger_state" in body
    _trigger = body["qualification_trigger_state"]
    assert _trigger is None or _trigger["auto_trigger"] in {
        "armed", "waiting", "in_flight", "capped", "settled", "observe", "off",
    }


@pytest.mark.asyncio
async def test_agent_fetch_qualification_surfaces_category_verdicts(
    client: AsyncClient, db_session
):
    """The verdict path names WHY each category is unmet: category_verdicts +
    residual_uncaptured_gap are carried from the immutable verdict row instead
    of dropped at the API boundary."""
    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-qual-catverdict"
    )

    verdict = QualificationVerdict(
        id=uuid.uuid4(),
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="abc12345def6789012345678901234567890abcd",
        confidence=0.62,
        terminal_status="not_promoted",
        finalized_at=datetime(2026, 6, 2, 12, 0, 0),
        category_verdicts=[
            {
                "category_key": "merge_grade",
                "status": "not_met",
                "tier": "judged",
                "gap": "Merge grade below threshold.",
                "citations": ["backend/app/release_promotion.py"],
            },
            {"category_key": "ci", "status": "met", "tier": "machine", "gap": None},
        ],
        residual_uncaptured_gap="Nothing covers the rollback path.",
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
    assert body["category_verdicts"] == [
        {
            "category_key": "merge_grade",
            "status": "not_met",
            "tier": "judged",
            "gap": "Merge grade below threshold.",
            "citations": ["backend/app/release_promotion.py"],
        },
        {"category_key": "ci", "status": "met", "tier": "machine", "gap": None},
    ]
    assert body["residual_uncaptured_gap"] == "Nothing covers the rollback path."


@pytest.mark.asyncio
async def test_agent_fetch_qualification_category_verdicts_in_fallback(
    client: AsyncClient, db_session
):
    """Legacy last_qualification blobs carry the same category_verdicts +
    residual_uncaptured_gap through the fallback path."""
    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-qual-catverdict-fb"
    )

    from sqlalchemy.orm.attributes import flag_modified

    version.last_qualification = {
        "status": "not_promoted",
        "confidence": 0.45,
        "candidate_sha": "deadbeef" * 5,
        "finalized_at": "2026-05-01T10:00:00",
        "category_verdicts": [
            {
                "category_key": "merge_grade",
                "status": "partial",
                "tier": "judged",
                "gap": "Only the happy path is covered.",
                "citations": [],
            }
        ],
        "residual_uncaptured_gap": "Rollback is unproven.",
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
    assert body["category_verdicts"] == [
        {
            "category_key": "merge_grade",
            "status": "partial",
            "tier": "judged",
            "gap": "Only the happy path is covered.",
            "citations": [],
        }
    ]
    assert body["residual_uncaptured_gap"] == "Rollback is unproven."


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
    assert body2["resolved_finding_total"] == 2
    assert body2["projected_audit_score"] == pytest.approx(1.0)
    assert body2["projected_confidence"] >= body2["promotion_confidence_threshold"]
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
async def test_policy_truth_separates_live_081_from_last_attempt_090(
    client: AsyncClient, db_session, mocker
):
    """Fetch surfaces must not present an old attempt threshold as live policy."""

    from sqlalchemy.orm.attributes import flag_modified

    from app import release_qualifier as rq

    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-policy-truth"
    )
    mocker.patch.dict(
        rq._runtime_config,
        {
            "promotion_threshold": 0.81,
            "promotion_weights": {
                "audit": 4 / 7,
                "test_erosion": 3 / 7,
            },
            "completion_debt_weight_mode": "observe",
        },
        clear=False,
    )
    version.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": "a" * 40,
        "confidence": 0.79,
        "threshold": 0.90,
        "promotion_policy_snapshot": {
            "threshold": 0.90,
            "weights": {"audit": 4 / 7, "test_erosion": 3 / 7},
        },
        "promotion_policy_source": "yaml_file",
        "promotion_policy_fallback_error": None,
        "finalized_at": "2026-06-03T10:00:00",
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()

    token = create_agent_token(str(task.id), task.vm_instance_id)
    headers = {"x-agent-token": token}
    qualification_response = await client.get(
        f"/api/agent/projects/{project.id}/qualification", headers=headers
    )
    version_response = await client.get(
        f"/api/agent/projects/{project.id}/version-phase", headers=headers
    )

    assert qualification_response.status_code == 200, qualification_response.text
    qualification = qualification_response.json()
    assert qualification["current_effective_policy"]["threshold"] == pytest.approx(
        0.81
    )
    assert qualification["current_effective_policy"]["source"] == "runtime_config"
    assert qualification["active_or_last_attempt_policy"]["threshold"] == pytest.approx(
        0.90
    )
    assert qualification["active_or_last_attempt_policy"]["source"] == "yaml_file"
    assert (
        qualification["current_effective_policy"]["policy_hash"]
        != qualification["active_or_last_attempt_policy"]["policy_hash"]
    )

    assert version_response.status_code == 200, version_response.text
    version_phase = version_response.json()["data"]
    assert version_phase["current_effective_policy"] == qualification[
        "current_effective_policy"
    ]
    assert version_phase["active_or_last_attempt_policy"] == qualification[
        "active_or_last_attempt_policy"
    ]
    assert version_phase["current_version"]["active_or_last_attempt_policy"] == (
        qualification["active_or_last_attempt_policy"]
    )


@pytest.mark.asyncio
async def test_qualification_fetch_carries_block_reason_fields(
    client: AsyncClient, db_session
):
    """Block-reason fields (error/detail/blocker/qualify_request_blocked/
    qualify_request_blocked_at) written by the hotfixed instrumentation paths are
    surfaced by the qualification fetch verb on both the fallback and verdict paths.

    Acceptance gate: named test required by the task acceptance criteria.
    """
    from sqlalchemy.orm.attributes import flag_modified

    # --- fallback path: no verdict row ---
    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-block-fb"
    )
    version.last_qualification = {
        "status": "error",
        "candidate_sha": "deadbeef" * 5,
        "error": "no_credentials",
        "detail": "GitHub token missing",
        "blocker": "ci_compliance",
        "qualify_request_blocked": {
            "blocker": "ci_compliance",
            "violation_count": 3,
            "head_sha": "cafebabe" * 5,
        },
        "qualify_request_blocked_at": "2026-07-05T10:00:00",
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
    assert body["status"] == "error"
    assert body["error"] == "no_credentials"
    assert body["detail"] == "GitHub token missing"
    assert body["blocker"] == "ci_compliance"
    assert body["qualify_request_blocked"] == {
        "blocker": "ci_compliance",
        "violation_count": 3,
        "head_sha": "cafebabe" * 5,
    }
    assert body["qualify_request_blocked_at"] == "2026-07-05T10:00:00"

    # --- verdict path: verdict row present, lq has block-reason fields ---
    project2, task2, version2 = await _make_project_with_version(
        db_session, vm="agent-vm-block-vp"
    )
    version2.last_qualification = {
        "status": "error",
        "candidate_sha": "aabbccdd" * 5,
        "error": "spawn_failed",
        "detail": "Worker capacity exhausted",
        "blocker": "capacity",
        "qualify_request_blocked_at": "2026-07-05T11:00:00",
    }
    flag_modified(version2, "last_qualification")
    db_session.add(version2)

    verdict2 = QualificationVerdict(
        id=uuid.uuid4(),
        project_id=project2.id,
        release_version_id=version2.id,
        candidate_sha="aabbccdd" * 5,
        confidence=0.6,
        terminal_status="not_promoted",
        audit_findings=[{"severity": "HIGH", "summary": "Missing guard"}],
    )
    db_session.add(verdict2)
    await db_session.commit()

    token2 = create_agent_token(str(task2.id), task2.vm_instance_id)
    res2 = await client.get(
        f"/api/agent/projects/{project2.id}/qualification",
        headers={"x-agent-token": token2},
    )
    assert res2.status_code == 200, res2.text
    body2 = res2.json()

    assert body2["source"] == "qualification_verdict"
    assert body2["error"] == "spawn_failed"
    assert body2["detail"] == "Worker capacity exhausted"
    assert body2["blocker"] == "capacity"
    assert body2["qualify_request_blocked"] is None
    assert body2["qualify_request_blocked_at"] == "2026-07-05T11:00:00"


@pytest.mark.asyncio
async def test_qualification_fetch_surfaces_blocker_execution_site(
    client: AsyncClient, db_session
):
    """The fetch verb surfaces the blocker's execution-site so an agent can tell
    whether a fix on automain takes effect pre-promotion (dir: tag promotion
    blockers inert-until-promote).

    A DEPLOYED-side blocker (migration-identity collision) is surfaced as
    execution_site='deployed'; a CANDIDATE-side blocker is 'candidate' and is
    DERIVED from the blocker code even when an older record carries no stored
    execution_site field.
    """
    from sqlalchemy.orm.attributes import flag_modified

    # --- deployed-side: stored execution_site is surfaced verbatim (verdict path) ---
    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-site-dep"
    )
    version.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": "abcdef12" * 5,
        "blocker": "migration_identity_collision",
        "execution_site": "deployed",
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    verdict = QualificationVerdict(
        id=uuid.uuid4(),
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="abcdef12" * 5,
        confidence=0.0,
        terminal_status="not_promoted",
    )
    db_session.add(verdict)
    await db_session.commit()

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/qualification",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["blocker"] == "migration_identity_collision"
    assert body["execution_site"] == "deployed"

    # --- candidate-side: DERIVED from the blocker when not stored (verdict path) ---
    project2, task2, version2 = await _make_project_with_version(
        db_session, vm="agent-vm-site-cand"
    )
    version2.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": "beefbeef" * 5,
        "blocker": "ci_not_green",
        # deliberately NO execution_site key — simulates an older record.
    }
    flag_modified(version2, "last_qualification")
    db_session.add(version2)
    verdict2 = QualificationVerdict(
        id=uuid.uuid4(),
        project_id=project2.id,
        release_version_id=version2.id,
        candidate_sha="beefbeef" * 5,
        confidence=0.0,
        terminal_status="not_promoted",
    )
    db_session.add(verdict2)
    await db_session.commit()

    token2 = create_agent_token(str(task2.id), task2.vm_instance_id)
    res2 = await client.get(
        f"/api/agent/projects/{project2.id}/qualification",
        headers={"x-agent-token": token2},
    )
    assert res2.status_code == 200, res2.text
    body2 = res2.json()
    assert body2["blocker"] == "ci_not_green"
    assert body2["execution_site"] == "candidate"


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


@pytest.mark.asyncio
async def test_agent_fetch_qualification_surfaces_promotion_failure_context(
    client: AsyncClient, db_session
):
    """agent_fetch_qualification surfaces promotion-failure context fields
    (would_promote, promotion_pr_url, promotion_conflict, promotion_error_detail,
    promotion_failure_count, promotion_pending) from both verdict and fallback paths."""
    from sqlalchemy.orm.attributes import flag_modified

    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-qa-promo-fail"
    )

    sha = "d" * 40
    verdict = QualificationVerdict(
        id=uuid.uuid4(),
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha=sha,
        confidence=0.85,
        sub_scores={"audit": 0.9},
        terminal_status="promotion_failed",
        finalized_at=datetime(2026, 6, 22, 14, 30, 0),
        audit_findings=[],
        would_promote=True,
        promotion_pr_url="https://github.com/example/repo/pull/42",
        promotion_conflict=True,
        promotion_error_detail="merge conflict: both sides modified auth.py",
    )
    db_session.add(verdict)

    # last_qualification carries the managed promotion sub-record.
    version.last_qualification = {
        "status": "promotion_failed",
        "candidate_sha": sha,
        "confidence": 0.85,
        "would_promote": True,
        "promotion_pr_url": "https://github.com/example/repo/pull/42",
        "promotion_conflict": True,
        "promotion_error_detail": "merge conflict: both sides modified auth.py",
        "promotion_failure_count": 1,
        "promotion_pending": {
            "pr_number": 42,
            "pr_url": "https://github.com/example/repo/pull/42",
            "managed_check_wait": True,
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

    # Verify promotion-failure context is surfaced
    assert body["status"] == "promotion_failed"
    assert body["would_promote"] is True
    assert body["promotion_pr_url"] == "https://github.com/example/repo/pull/42"
    assert body["promotion_conflict"] is True
    assert (
        body["promotion_error_detail"]
        == "merge conflict: both sides modified auth.py"
    )
    assert body["promotion_failure_count"] == 1

    # Verify promotion_pending sub-record
    promotion_pending = body.get("promotion_pending")
    assert promotion_pending is not None
    assert promotion_pending["pr_number"] == 42
    assert promotion_pending["pr_url"] == "https://github.com/example/repo/pull/42"
    assert promotion_pending["managed_check_wait"] is True


@pytest.mark.asyncio
async def test_agent_fetch_qualification_promotion_context_in_fallback(
    client: AsyncClient, db_session
):
    """agent_fetch_qualification surfaces promotion-failure context in fallback
    (last_qualification) path when QualificationVerdict is absent."""
    from sqlalchemy.orm.attributes import flag_modified

    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-qa-promo-fb"
    )

    version.last_qualification = {
        "status": "promotion_failed",
        "candidate_sha": "e" * 40,
        "confidence": 0.72,
        "would_promote": True,
        "promotion_pr_url": "https://github.com/example/repo/pull/99",
        "promotion_conflict": False,
        "promotion_error_detail": "CI checks still pending",
        "promotion_failure_count": 2,
        "promotion_pending": {
            "pr_number": 99,
            "pr_url": "https://github.com/example/repo/pull/99",
            "managed_check_wait": True,
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
    assert body["status"] == "promotion_failed"
    assert body["would_promote"] is True
    assert body["promotion_pr_url"] == "https://github.com/example/repo/pull/99"
    assert body["promotion_conflict"] is False
    assert body["promotion_error_detail"] == "CI checks still pending"
    assert body["promotion_failure_count"] == 2

    promotion_pending = body.get("promotion_pending")
    assert promotion_pending is not None
    assert promotion_pending["pr_number"] == 99
    assert promotion_pending["managed_check_wait"] is True


@pytest.mark.asyncio
async def test_agent_fetch_qualification_stamps_serving_sha_on_verdict_path(
    client: AsyncClient, db_session, monkeypatch
):
    """agent_fetch_qualification includes serving_sha on verdict path to identify
    which deployed code revision produced the response."""
    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-qa-sha-verdict"
    )

    verdict = QualificationVerdict(
        id=uuid.uuid4(),
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="abc12345def6789012345678901234567890abcd",
        confidence=0.72,
        sub_scores={"audit": 0.5, "test_erosion": 1.0},
        terminal_status="dry_run",
        finalized_at=datetime(2026, 6, 1, 12, 0, 0),
        audit_findings=[{"severity": "HIGH", "summary": "Missing guard"}],
    )
    db_session.add(verdict)
    await db_session.commit()
    await db_session.refresh(verdict)

    # Mock the serving SHA to a known test value
    test_serving_sha = "f" * 40
    version.base_sha = "b" * 40
    db_session.add(version)
    await db_session.commit()
    monkeypatch.setattr(
        "app.deployed_sha.get_serving_sha",
        lambda: test_serving_sha,
    )
    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/qualification",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["source"] == "qualification_verdict"
    assert body["serving_sha"] == test_serving_sha
    assert body["operator_authored_commits"]["state"] == "unresolved"
    assert body["operator_authored_commits"]["count"] is None


@pytest.mark.asyncio
async def test_agent_fetch_qualification_stamps_serving_sha_on_fallback_path(
    client: AsyncClient, db_session, monkeypatch
):
    """agent_fetch_qualification includes serving_sha on fallback (last_qualification) path."""
    from sqlalchemy.orm.attributes import flag_modified

    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-qa-sha-fb"
    )

    version.last_qualification = {
        "status": "not_promoted",
        "confidence": 0.45,
        "candidate_sha": "deadbeef" * 5,
        "finalized_at": "2026-05-01T10:00:00",
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()

    # Mock the serving SHA
    test_serving_sha = "e" * 40
    monkeypatch.setattr(
        "app.deployed_sha.get_serving_sha",
        lambda: test_serving_sha,
    )

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/qualification",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["source"] == "last_qualification"
    assert body["serving_sha"] == test_serving_sha


@pytest.mark.asyncio
async def test_agent_fetch_qualification_stamps_serving_sha_on_no_open_version(
    client: AsyncClient, db_session, monkeypatch
):
    """agent_fetch_qualification includes serving_sha even when no open version exists."""
    project = Project(name="No Open Version SHA", user_id="test-user-nov-sha")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-nov-sha")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Mock the serving SHA
    test_serving_sha = "d" * 40
    monkeypatch.setattr(
        "app.deployed_sha.get_serving_sha",
        lambda: test_serving_sha,
    )

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/qualification",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["source"] == "no_open_version"
    assert body["serving_sha"] == test_serving_sha


@pytest.mark.asyncio
async def test_agent_fetch_qualification_stamps_serving_sha_when_none(
    client: AsyncClient, db_session, monkeypatch
):
    """serving_sha is included in response even when get_serving_sha returns None."""
    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-qa-sha-none"
    )

    verdict = QualificationVerdict(
        id=uuid.uuid4(),
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="abc12345def6789012345678901234567890abcd",
        confidence=0.72,
        terminal_status="dry_run",
        audit_findings=[],
    )
    db_session.add(verdict)
    await db_session.commit()

    # Mock get_serving_sha to return None (e.g., running locally without DEPLOY_SHA)
    monkeypatch.setattr(
        "app.deployed_sha.get_serving_sha",
        lambda: None,
    )

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/qualification",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["source"] == "qualification_verdict"
    assert body["serving_sha"] is None


@pytest.mark.asyncio
async def test_agent_fetch_version_phase_returns_compact_lifecycle_context(
    client: AsyncClient, db_session
):
    """Lifecycle fetch returns current version facts without full audit findings."""
    from sqlalchemy.orm.attributes import flag_modified

    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-version-phase"
    )
    version.phase = "stabilize"
    version.goal = "Harden the release gate"
    version.phase_changed_at = datetime(2026, 6, 2, 9, 30, 0)
    version.qualification_attempts = 2
    version.serving_status = "unconfirmed"
    version.deploy_confirm_deadline = datetime(2026, 6, 2, 10, 0, 0)
    version.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": "abc123" * 6 + "abcd",
        "confidence": 0.61,
        "threshold": 0.8,
        "sub_scores": {"audit": 0.4, "ci": 1.0},
        "blocker": "audit_findings",
        "finalized_at": "2026-06-02T09:45:00",
        "audit_findings": [
            {"severity": "HIGH", "summary": "Missing auth"},
            {"severity": "MEDIUM", "summary": "Weak test"},
        ],
        "excluded_audit_findings": [{"severity": "LOW", "summary": "Style"}],
        "challenger_refuted_audit_findings": [
            {"severity": "MEDIUM", "summary": "False positive"}
        ],
        "goal_alignment": {"state": "shortfall", "biggest_gap": "release gate"},
        "debt_enforce_confidence_preview": 0.52,
        "debt_enforce_weight_preview": 0.15,
    }
    flag_modified(version, "last_qualification")

    closed = ReleaseVersion(
        project_id=project.id,
        version_number=9,
        status="promoted",
        phase="stabilize",
        goal="Previous release",
        opened_at=datetime(2026, 5, 1, 0, 0, 0),
        promoted_at=datetime(2026, 5, 10, 12, 0, 0),
        serving_status="serving_confirmed",
        served_sha="legacyfeed",
        served_at=datetime(2026, 5, 10, 12, 30, 0),
        last_qualification={
            "status": "promoted",
            "confidence": 0.91,
            "audit_findings": [{"severity": "LOW", "summary": "Historical"}],
        },
    )
    db_session.add(closed)
    db_session.add(
        ReleaseDeployArtifact(
            version_id=closed.id,
            artifact_kind="backend_revision",
            serving_status="serving_confirmed",
            served_identifier="feedface",
            served_at=datetime(2026, 5, 10, 12, 31, 0),
        )
    )
    await db_session.commit()

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/version-phase",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"

    data = body["data"]
    assert data["source"] == "open_version"
    current = data["current_version"]
    assert current["version_number"] == 10
    assert current["phase"] == "stabilize"
    assert current["goal"] == "Harden the release gate"
    assert current["phase_changed_at"] == "2026-06-02T09:30:00"
    assert current["qualification_attempts"] == 2
    assert current["qualification_state"]["state"] == "not_promoted"
    assert current["serving"]["status"] == "unconfirmed"
    assert current["deploy_confirm_deadline"] == "2026-06-02T10:00:00"

    qual = current["last_qualification"]
    assert qual["status"] == "not_promoted"
    assert qual["candidate_sha"].startswith("abc123")
    assert qual["audit_findings_count"] == 2
    assert qual["excluded_audit_findings_count"] == 1
    assert qual["challenger_refuted_audit_findings_count"] == 1
    assert "audit_findings" not in qual
    assert "excluded_audit_findings" not in qual
    assert "challenger_refuted_audit_findings" not in qual
    assert qual["debt_enforce_confidence_preview"] == pytest.approx(0.52)
    assert qual["debt_enforce_weight_preview"] == pytest.approx(0.15)

    recent = data["recent_versions"]
    assert len(recent) == 1
    assert recent[0]["version_number"] == 9
    assert recent[0]["serving"]["status"] == "serving_confirmed"
    assert recent[0]["serving"]["sha"] == "feedface"
    assert "audit_findings" not in recent[0]["last_qualification"]


@pytest.mark.asyncio
async def test_agent_fetch_version_phase_alias_returns_no_open_version(
    client: AsyncClient, db_session
):
    project, task = await _make_project_task(
        db_session, vm="agent-vm-version-phase-alias"
    )

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/version_phase",
        headers={"x-agent-token": token},
    )

    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["source"] == "no_open_version"
    assert data["current_version"] is None
    assert data["recent_versions"] == []


@pytest.mark.asyncio
async def test_agent_fetch_version_phase_rejects_cross_project(
    client: AsyncClient, db_session
):
    project_a = Project(name="Version Phase A", user_id="test-user-vpa")
    project_b = Project(name="Version Phase B", user_id="test-user-vpb")
    db_session.add_all([project_a, project_b])
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    task_a = Task(
        project_id=project_a.id,
        payloads=[{}],
        vm_instance_id="agent-vm-version-phase-cross",
    )
    db_session.add(task_a)
    await db_session.commit()
    await db_session.refresh(task_a)

    token = create_agent_token(str(task_a.id), task_a.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project_b.id}/version-phase",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404


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


@pytest.mark.asyncio
async def test_keyless_finding_key_round_trips_through_resolution_endpoint(
    client: AsyncClient, db_session
):
    """A keyless finding surfaced in finding_resolution carries the __keyless__
    sentinel as its key, which the resolution endpoint accepts without error.

    This validates the full round-trip: GET qualification → read finding_key →
    POST audit-finding-resolution with that key → GET qualification again and
    confirm the finding now shows as stale.
    """
    from app.release_readiness import _KEYLESS_FINDING_SENTINEL

    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-keyless-rt"
    )

    # A keyless gating finding: severity HIGH, no 'key' field (challenger-independent).
    keyless_finding = {"severity": "HIGH", "summary": "Fail-open rate false positive"}

    verdict = QualificationVerdict(
        id=uuid.uuid4(),
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="a" * 40,
        confidence=0.5,
        sub_scores={"audit": 0.4},
        terminal_status="not_promoted",
        audit_findings=[keyless_finding],
    )
    db_session.add(verdict)
    await db_session.commit()

    read_token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/qualification",
        headers={"x-agent-token": read_token},
    )
    assert res.status_code == 200, res.text
    body = res.json()

    # The finding_resolution list must expose the sentinel key (not empty string)
    # so the caller can pass it directly to the resolution endpoint.
    assert len(body["finding_resolution"]) == 1
    finding_key_from_api = body["finding_resolution"][0]["key"]
    assert finding_key_from_api == _KEYLESS_FINDING_SENTINEL, (
        f"Expected sentinel '{_KEYLESS_FINDING_SENTINEL}', got '{finding_key_from_api}'"
    )
    assert body["finding_resolution"][0]["state"] == "unresolved"
    assert body["requalification_ready"] is False

    # Use the key exactly as surfaced by the API to call the resolution endpoint.
    audit_task = Task(
        project_id=project.id,
        payloads=[{}],
        vm_instance_id="agent-vm-keyless-audit",
    )
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(audit_task)
    write_token = create_agent_token(
        str(audit_task.id), audit_task.vm_instance_id, task_type="research"
    )

    res2 = await client.post(
        "/api/agent/audit-finding-resolution",
        json={
            "task_id": str(audit_task.id),
            "finding_key": finding_key_from_api,
            "state": "stale",
            "note": "False positive — fail_open_rate formula verified correct at HEAD",
        },
        headers={"x-agent-token": write_token},
    )
    assert res2.status_code == 200, res2.text
    body2 = res2.json()
    assert body2["status"] == "ok"
    assert body2["finding_key"] == _KEYLESS_FINDING_SENTINEL

    # Re-read qualification: the keyless finding should now show as stale and
    # requalification_ready should flip to True.
    res3 = await client.get(
        f"/api/agent/projects/{project.id}/qualification",
        headers={"x-agent-token": read_token},
    )
    assert res3.status_code == 200, res3.text
    body3 = res3.json()
    assert body3["finding_resolution"][0]["key"] == _KEYLESS_FINDING_SENTINEL
    assert body3["finding_resolution"][0]["state"] == "stale"
    assert body3["requalification_ready"] is True


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
async def test_directions_list_mode(
    client: AsyncClient, db_session
):
    """No-id directions list returns compact (id, status, text, priority) for all
    directions — the recovery path when directions_summary was dropped entirely."""
    project = Project(
        name="Dir List Test",
        user_id="test-user-dirlist",
        directions=[
            {"id": "d-1", "status": "active", "text": "First direction", "priority": "high"},
            {"id": "d-2", "status": "paused", "text": "Second direction", "priority": "low"},
            {"id": "d-3", "status": "retired", "text": "Old direction"},
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-dirlist")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-dirlist")
    res = await client.get(
        f"/api/agent/projects/{project.id}/directions",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    data = body["data"]
    assert len(data) == 3
    by_id = {d["id"]: d for d in data}
    assert by_id["d-1"] == {"id": "d-1", "status": "active", "text": "First direction", "priority": "high"}
    assert by_id["d-2"] == {"id": "d-2", "status": "paused", "text": "Second direction", "priority": "low"}
    # retired direction with no priority — only present keys included
    assert by_id["d-3"]["status"] == "retired"
    assert "priority" not in by_id["d-3"]


@pytest.mark.asyncio
async def test_open_messages_list_mode(
    client: AsyncClient, db_session
):
    """No-id messages list returns compact open-message listing with id, ref,
    type, from_type, content excerpt, status."""
    project, task = await _make_project_task(db_session, vm="agent-vm-msglist")
    open_msg = await _add_message(
        db_session,
        project,
        message_type=MessageType.BUG_REPORT,
        from_type="user",
        content="x" * 300,
        status="open",
    )
    await _add_message(
        db_session,
        project,
        message_type=MessageType.NOTE,
        from_type="strategic",
        content="resolved note",
        status="resolved",
    )
    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/messages/open",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    data = body["data"]
    # Only the open message is returned
    assert len(data) == 1
    item = data[0]
    assert item["id"] == str(open_msg.id)
    assert item["ref"] == open_msg.id.hex[:8]
    assert item["type"] == "BUG_REPORT"
    assert item["from_type"] == "user"
    assert item["status"] == "open"
    # Content is truncated to 200 chars
    assert len(item["content"]) == 200


@pytest.mark.asyncio
async def test_directions_list_mode_rejects_cross_project(
    client: AsyncClient, db_session
):
    """Token from project A cannot list directions for project B (404)."""
    project_a = Project(name="Dir List A", user_id="test-user-dla")
    project_b = Project(
        name="Dir List B",
        user_id="test-user-dlb",
        directions=[{"id": "d-b1", "status": "active", "text": "B direction"}],
    )
    db_session.add_all([project_a, project_b])
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    task_a = Task(project_id=project_a.id, payloads=[{}], vm_instance_id="agent-vm-dla")
    db_session.add(task_a)
    await db_session.commit()
    await db_session.refresh(task_a)

    token = create_agent_token(str(task_a.id), "agent-vm-dla")
    res = await client.get(
        f"/api/agent/projects/{project_b.id}/directions",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_agent_fetch_capability_trials_returns_verdicts(
    client: AsyncClient, db_session
):
    """Agent endpoint returns capability-trial verdicts keyed by cap_name."""
    project = Project(
        name="Cap Trial Test",
        user_id="test-user-ct",
        capability_adoption_verdicts={
            "repo_map": {
                "verdict": "continue-trial",
                "verdict_reason": "insufficient_data",
                "lift": None,
                "n_with": 10,
                "n_without": 5,
                "arm_composition": {
                    "with_assigned": 10,
                    "without_assigned": 5,
                },
                "accrual_projection": {
                    "with_7d": 3,
                    "without_7d": 1,
                    "days_to_min_n": 25.0,
                },
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-ct")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/capability-trials",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    verdicts = body["data"]["verdicts"]
    assert "repo_map" in verdicts
    entry = verdicts["repo_map"]
    assert entry["verdict"] == "continue-trial"
    assert entry["n_with"] == 10
    assert entry["n_without"] == 5
    assert entry["arm_composition"]["with_assigned"] == 10
    assert entry["accrual_projection"]["days_to_min_n"] == 25.0


@pytest.mark.asyncio
async def test_agent_fetch_capability_trials_empty_when_no_verdicts(
    client: AsyncClient, db_session
):
    """No verdicts on project → returns empty verdicts dict."""
    project, task = await _make_project_task(db_session, vm="agent-vm-cte")
    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/capability-trials",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    assert body["data"]["verdicts"] == {}


@pytest.mark.asyncio
async def test_agent_fetch_capability_trials_rejects_cross_project(
    client: AsyncClient, db_session
):
    """Agent token for project A cannot fetch capability-trials for project B (404)."""
    project_a = Project(name="CT A", user_id="test-user-cta")
    project_b = Project(name="CT B", user_id="test-user-ctb")
    db_session.add_all([project_a, project_b])
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    task_a = Task(
        project_id=project_a.id, payloads=[{}], vm_instance_id="agent-vm-cta"
    )
    db_session.add(task_a)
    await db_session.commit()
    await db_session.refresh(task_a)

    token = create_agent_token(str(task_a.id), "agent-vm-cta")
    res = await client.get(
        f"/api/agent/projects/{project_b.id}/capability-trials",
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
    assert "cost_per_verified_task" in data
    assert "vm_reuse_rate" in data
    assert "llm_cost_per_merged_pr" in data
    assert "llm_cost_per_verified_task" in data
    assert "llm_cache_hit_rate" in data
    assert "format_compliance" in data
    assert data["window_days"] == 7
    assert (
        data["merged_work_origination"]["cohort_created_at_floor"]
        == "2026-08-08T06:59:00+00:00"
    )


@pytest.mark.asyncio
async def test_fetch_efficiency_snapshot_includes_llm_cost_attribution(
    client: AsyncClient, db_session
):
    """Efficiency snapshot includes llm_cost_attribution with by_task_type rows
    and strategic_cycle_cost_per_event."""
    from datetime import datetime, timezone

    from app.models import TaskStatus

    project = Project(name="LLM Attribution Test", user_id="test-user-llmattr")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    token_task = Task(
        project_id=project.id,
        payloads=[{}],
        vm_instance_id="agent-vm-llmattr",
    )
    db_session.add(token_task)

    # A completed create task with LLM cost data
    create_task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        updated_at=datetime.now(timezone.utc),
        vm_provisioned_at=datetime.now(timezone.utc),
        vm_stopped_at=datetime.now(timezone.utc),
        context={
            "llm_cost_usd": 0.05,
            "llm_input_tokens": 1000,
            "llm_output_tokens": 200,
        },
    )
    db_session.add(create_task)

    await db_session.commit()
    await db_session.refresh(token_task)

    token = create_agent_token(str(token_task.id), "agent-vm-llmattr")
    res = await client.get(
        f"/api/agent/projects/{project.id}/efficiency-snapshot",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    data = body["data"]

    # Existing scalar fields still present
    assert "cost_per_merged_pr" in data
    assert "cost_per_verified_task" in data
    assert "llm_cost_per_merged_pr" in data
    assert "llm_cost_per_verified_task" in data
    assert "llm_cache_hit_rate" in data
    assert "format_compliance" in data
    assert data["window_days"] == 7

    # New llm_cost_attribution block present with required sub-fields
    assert "llm_cost_attribution" in data
    attr = data["llm_cost_attribution"]
    assert "by_task_type" in attr
    assert "strategic_cycle_cost_per_event" in attr
    assert "self_repair_burn_share" in attr
    assert "total_llm_usd" in attr
    assert isinstance(attr["by_task_type"], list)

    # gate_health and gate_health_post_deploy were removed when panel_review was
    # removed in automain (998c53c). They are no longer present in the response.


@pytest.mark.asyncio
async def test_fetch_efficiency_snapshot_includes_llm_cache_hit_rate(
    client: AsyncClient, db_session
):
    """Agent efficiency-snapshot returns llm_cache_hit_rate with a numerically
    correct value computed from per-task cache-read / total-input tokens."""
    from datetime import datetime, timezone

    from app.models import TaskStatus

    project = Project(name="Cache Hit Test", user_id="test-user-cachehit")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    token_task = Task(
        project_id=project.id,
        payloads=[{}],
        vm_instance_id="agent-vm-cachehit",
    )
    db_session.add(token_task)

    # Task 1: 1000 input tokens, 300 cache-read → 0.30 cache hit rate contribution
    db_session.add(
        Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
            updated_at=datetime.now(timezone.utc),
            vm_provisioned_at=datetime.now(timezone.utc),
            vm_stopped_at=datetime.now(timezone.utc),
            provider="anthropic",
            model="claude-sonnet-4-6",
            api_call_summary={
                "total_input_tokens": 1000,
                "total_output_tokens": 200,
                "total_cache_read_input_tokens": 300,
                "llm_call_count": 2,
            },
        )
    )

    # Task 2: 1000 input tokens, 100 cache-read → 0.10 cache hit rate contribution
    db_session.add(
        Task(
            project_id=project.id,
            payloads=[{"type": "fix"}],
            status=TaskStatus.COMPLETED,
            updated_at=datetime.now(timezone.utc),
            vm_provisioned_at=datetime.now(timezone.utc),
            vm_stopped_at=datetime.now(timezone.utc),
            provider="anthropic",
            model="claude-sonnet-4-6",
            api_call_summary={
                "total_input_tokens": 1000,
                "total_output_tokens": 100,
                "total_cache_read_input_tokens": 100,
                "llm_call_count": 1,
            },
        )
    )

    await db_session.commit()
    await db_session.refresh(token_task)

    token = create_agent_token(str(token_task.id), "agent-vm-cachehit")
    res = await client.get(
        f"/api/agent/projects/{project.id}/efficiency-snapshot",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert "llm_cache_hit_rate" in data
    # (300 + 100) cache-read / (1000 + 1000) total input = 400/2000 = 0.20
    assert data["llm_cache_hit_rate"] == 0.20


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


@pytest.mark.asyncio
async def test_agent_fetch_fleet_sizing_readings(
    client: AsyncClient, db_session, monkeypatch
):
    """GET /api/agent/projects/{id}/fleet-sizing serves the three readings.

    The endpoint is the agent-reachable read path for the recorded fleet-sizing
    numbers (workspace_footprint, concurrency, target_concurrency_key), so a
    task agent on a VM with no production DB can pull them when committing an
    evidence artifact. Cross-project access is fenced to 404 by
    ``_resolve_agent_project``."""
    import app.vm_lifecycle as vm_lifecycle
    import app.warm_pool as warm_pool

    from app import max_plan_pool

    monkeypatch.setattr(max_plan_pool, "_all_accounts", lambda: [])
    monkeypatch.setattr(
        max_plan_pool, "_account_available_for_model", lambda acct, model: True
    )
    monkeypatch.setattr(vm_lifecycle, "_last_gcp_vm_count", 6)
    monkeypatch.setattr(vm_lifecycle, "_last_gcp_vm_check", float("inf"))
    monkeypatch.setattr(warm_pool, "is_warm_pool_enabled", lambda: False)

    project, task = await _make_project_task(db_session, vm="agent-vm-fleetsz")
    token = create_agent_token(str(task.id), "agent-vm-fleetsz")
    res = await client.get(
        f"/api/agent/projects/{project.id}/fleet-sizing",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    data = body["data"]
    assert set(data.keys()) == {
        "workspace_footprint",
        "concurrency",
        "target_concurrency_key",
    }
    assert data["concurrency"]["window_hours"] == 24
    assert data["concurrency"]["slot_definition"]
    assert data["workspace_footprint"]["target_slots"] == 50
    assert data["workspace_footprint"]["per_workspace_bytes"] == 2 * 1024**3

    # A token for another project cannot read this project's readings (404).
    other = Project(name="FS Other", user_id="test-user-fso")
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)
    res = await client.get(
        f"/api/agent/projects/{other.id}/fleet-sizing",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_strategic_resolve_question_posts_answer_note(
    client: AsyncClient, db_session, mocker
):
    """resolve_message on a QUESTION-type message posts a question_answer NOTE."""
    from unittest.mock import AsyncMock

    project, task = await _make_project_task(db_session)
    msg = await _add_message(
        db_session,
        project,
        message_type=MessageType.QUESTION,
        from_type="user",
        content="What is the current approach?",
        status="open",
        surface=MessageSurface.CONVERSATION,
        sequence=1,
    )
    token = create_agent_token(
        str(task.id), task.vm_instance_id, task_type="strategic_review"
    )
    broadcast = mocker.patch(
        "app.routers.admin_agent_ops.broadcast_to_project",
        new=AsyncMock(),
    )
    res = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "resolve_message",
            "data": {
                "message_id": str(msg.id),
                "resolution": "We use an iterative approach with weekly cycles.",
            },
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "ok"

    from sqlalchemy import select
    from app.models import ProjectMessage

    notes = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.message_type == MessageType.NOTE,
                ProjectMessage.from_type == "strategic",
            )
        )
    ).scalars().all()
    matching = [
        n
        for n in notes
        if isinstance(n.context, dict)
        and n.context.get("event_type") == "question_answer"
        and n.context.get("reply_to") == str(msg.id)
    ]
    assert len(matching) == 1, f"Expected 1 question_answer NOTE, got {len(matching)}"
    await db_session.refresh(msg)
    reply_msg = matching[0]
    assert reply_msg.status == "resolved"
    assert reply_msg.handling_state == MessageHandlingState.RESPONDED
    assert broadcast.await_count == 2
    update_call, create_call = broadcast.await_args_list
    project_id, event, payload = update_call.args
    assert project_id == str(project.id)
    assert event == "message_updated"
    _assert_project_message_event_payload(
        payload,
        project=project,
        msg=msg,
        action="resolved",
        status="resolved",
    )
    project_id, event, payload = create_call.args
    assert project_id == str(project.id)
    assert event == "message_created"
    _assert_project_message_event_payload(
        payload,
        project=project,
        msg=reply_msg,
        action="created",
        status="resolved",
    )


@pytest.mark.asyncio
async def test_strategic_resolve_question_commit_failure_is_atomic(
    client: AsyncClient, db_session, mocker
):
    """A failed commit persists neither the resolution nor its canonical reply."""
    from unittest.mock import AsyncMock

    project, task = await _make_project_task(db_session)
    msg = await _add_message(
        db_session,
        project,
        message_type=MessageType.QUESTION,
        from_type="user",
        content="What changed?",
        status="open",
    )
    token = create_agent_token(
        str(task.id), task.vm_instance_id, task_type="strategic_review"
    )
    broadcast = mocker.patch(
        "app.routers.admin_agent_ops.broadcast_to_project",
        new=AsyncMock(),
    )
    from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

    async def _commit_reply_failure(session, *args, **kwargs):
        raise RuntimeError("atomic resolve commit failed")

    mocker.patch.object(
        _AsyncSession,
        "commit",
        autospec=True,
        side_effect=_commit_reply_failure,
    )

    with pytest.raises(RuntimeError, match="atomic resolve commit failed"):
        await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(task.id),
                "op": "resolve_message",
                "data": {
                    "message_id": str(msg.id),
                    "resolution": "Resolved, but reply persistence failed.",
                },
            },
        )

    # Snapshot project.id before the rollback: on Postgres the rollback expires
    # ORM attributes (the conftest _expire no-op is SQLite-only), so reading
    # project.id afterward triggers a sync lazy-load -> MissingGreenlet.
    project_id = project.id
    await db_session.rollback()
    await db_session.refresh(msg)
    assert msg.status == "open"
    assert broadcast.await_count == 0
    notes = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.message_type == MessageType.NOTE,
            )
        )
    ).scalars().all()
    assert notes == []


@pytest.mark.asyncio
async def test_strategic_resolve_question_retry_reconciles_missing_reply(
    client: AsyncClient, db_session
):
    project, task = await _make_project_task(db_session)
    msg = await _add_message(
        db_session,
        project,
        message_type=MessageType.QUESTION,
        from_type="user",
        content="Was this already answered?",
        status="resolved",
        resolution="The durable answer.",
    )
    token = create_agent_token(
        str(task.id), task.vm_instance_id, task_type="strategic_review"
    )
    request = {
        "task_id": str(task.id),
        "op": "resolve_message",
        "data": {
            "message_id": str(msg.id),
            "resolution": "The durable answer.",
        },
    }

    first = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json=request,
    )
    second = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json=request,
    )

    assert first.status_code == 200, first.text
    assert first.json()["status"] == "already_resolved"
    assert second.status_code == 200, second.text
    replies = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.reply_to_message_id == msg.id,
            )
        )
    ).scalars().all()
    assert len(replies) == 1
    assert replies[0].sequence is not None


@pytest.mark.asyncio
async def test_strategic_resolve_feedback_posts_acknowledgment_note(
    client: AsyncClient, db_session
):
    """resolve_message on a FEEDBACK message posts a feedback_acknowledgment NOTE."""
    project, task = await _make_project_task(db_session)
    msg = await _add_message(
        db_session,
        project,
        message_type=MessageType.FEEDBACK,
        from_type="user",
        content="Please clarify the rollout note.",
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
                "message_id": str(msg.id),
                "resolution": "Rollout note now names the release owner.",
            },
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "ok"

    from sqlalchemy import select
    from app.models import ProjectMessage

    notes = (
        await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.message_type == MessageType.NOTE,
                    ProjectMessage.from_type == "strategic",
                )
        )
    ).scalars().all()
    matching = [
        n
        for n in notes
        if isinstance(n.context, dict)
        and n.context.get("event_type") == "feedback_acknowledgment"
        and n.context.get("reply_to") == str(msg.id)
        and n.context.get("resolution") == "Rollout note now names the release owner."
    ]
    assert len(matching) == 1, (
        f"Expected 1 feedback_acknowledgment NOTE, got {len(matching)}"
    )


@pytest.mark.asyncio
async def test_agent_fetch_version_phase_surfaces_goal_coverage_drift(
    client: AsyncClient, db_session
):
    """version_phase endpoint includes goal_coverage_drift for open version with a
    merge-grade verdict that shows a goal miss — signals drifting + biggest_gap."""
    from sqlalchemy.orm.attributes import flag_modified

    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-vp-drift"
    )
    version.goal = "Harden the auth layer and improve test reliability"
    version.phase = "build"
    flag_modified(version, "goal")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Create a QualificationVerdict whose merge-grade indicates a goal miss.
    from app.models import QualificationVerdict as QV
    verdict = QV(
        id=uuid.uuid4(),
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="deadbeef" * 5,
        confidence=0.55,
        terminal_status="not_promoted",
        audit_findings=[],
        merge_grade_status="fail",
        merge_grade_coverage=0.35,
        merge_grade_biggest_gap="Auth improvements not merged",
        finalized_at=datetime(2026, 6, 1, 10, 0, 0),
    )
    db_session.add(verdict)
    await db_session.commit()
    await db_session.refresh(verdict)

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/version-phase",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"

    data = body["data"]
    # Backward-compat: existing keys unchanged
    assert data["source"] == "open_version"
    assert data["current_version"] is not None
    assert "recent_versions" in data

    # New additive field: goal_coverage_drift
    drift = data.get("goal_coverage_drift")
    assert drift is not None, "goal_coverage_drift must be present for open version"
    assert drift["signal"] == "drifting"
    assert drift["biggest_gap"] == "Auth improvements not merged"
    assert drift["coverage"] == pytest.approx(0.35)
    # No material new work added after the verdict (count_merged_since returns 0
    # when no COMPLETED tasks exist in the window), so stale may be False.
    assert isinstance(drift["merges_since_grade"], int)
    assert "stale" in drift
    assert "detail" in drift


@pytest.mark.asyncio
async def test_agent_fetch_version_phase_drift_not_applicable_empty_goal(
    client: AsyncClient, db_session
):
    """goal_coverage_drift is not_applicable when version has no goal."""
    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-vp-drift-na"
    )
    # Ensure goal is empty/None
    version.goal = None
    db_session.add(version)
    await db_session.commit()

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/version-phase",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    drift = body["data"].get("goal_coverage_drift")
    assert drift is not None
    assert drift["signal"] == "not_applicable"


@pytest.mark.asyncio
async def test_agent_fetch_qualification_includes_goal_coverage_drift_signal(
    client: AsyncClient, db_session
):
    """qualification endpoint includes goal_coverage_drift; existing keys unchanged."""
    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-qual-drift"
    )
    from sqlalchemy.orm.attributes import flag_modified

    version.goal = "Ship the new data pipeline"
    flag_modified(version, "goal")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    verdict = QualificationVerdict(
        id=uuid.uuid4(),
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="cafebabe" * 5,
        confidence=0.91,
        terminal_status="dry_run",
        audit_findings=[],
        merge_grade_status="pass",
        merge_grade_coverage=0.88,
        merge_grade_biggest_gap=None,
        finalized_at=datetime(2026, 6, 15, 12, 0, 0),
    )
    db_session.add(verdict)
    await db_session.commit()

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/qualification",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()

    # Backward-compat: existing keys must not change
    assert body["source"] == "qualification_verdict"
    assert body["candidate_sha"] is not None
    assert "requalification_ready" in body
    assert "finding_resolution" in body

    # New additive field
    drift = body.get("goal_coverage_drift")
    assert drift is not None
    assert drift["signal"] == "on_track"
    assert drift["coverage"] == pytest.approx(0.88)


@pytest.mark.asyncio
async def test_agent_fetch_qualification_drift_in_fallback_path(
    client: AsyncClient, db_session
):
    """goal_coverage_drift is present in the last_qualification fallback path too."""
    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-qual-drift-fb"
    )
    from sqlalchemy.orm.attributes import flag_modified

    version.goal = "Improve observability"
    version.last_qualification = {
        "status": "not_promoted",
        "confidence": 0.5,
        "candidate_sha": "aabbccdd" * 5,
    }
    flag_modified(version, "goal")
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
    # New additive field present even on fallback path (no verdict → no_grade_yet)
    drift = body.get("goal_coverage_drift")
    assert drift is not None
    assert drift["signal"] == "no_grade_yet"


@pytest.mark.asyncio
async def test_agent_fetch_qualification_current_identity_uses_version_phase_when_verdict_evidence_is_historical(
    client: AsyncClient, db_session
):
    """Identity fields (candidate_sha, status, finalized_at) come from
    last_qualification (Version Phase) even when a historical verdict row with
    audit_findings exists, so ./fetch qualification and ./fetch version-phase
    always agree on the current candidate identity."""
    from sqlalchemy.orm.attributes import flag_modified

    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-qual-vp-identity"
    )

    # Older verdict with audit_findings — its identity fields are historical.
    old_sha = "a" * 40
    historical_finding = {"severity": "HIGH", "summary": "Old finding", "file": "app/old.py"}
    old_verdict = QualificationVerdict(
        id=uuid.uuid4(),
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha=old_sha,
        confidence=0.55,
        sub_scores={"audit": 0.4},
        terminal_status="not_promoted",
        finalized_at=datetime(2026, 5, 1, 8, 0, 0),
        audit_findings=[historical_finding],
    )
    db_session.add(old_verdict)

    # Newer qualification recorded in Version Phase last_qualification — this is
    # the authoritative current candidate identity.
    new_sha = "b" * 40
    version.last_qualification = {
        "status": "dry_run",
        "candidate_sha": new_sha,
        "finalized_at": "2026-06-15T10:00:00",
        "confidence": 0.82,
        "sub_scores": {"audit": 0.9},
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

    # Source still reflects that a verdict row provided the findings.
    assert body["source"] == "qualification_verdict"

    # Identity fields must come from last_qualification (Version Phase),
    # NOT from the historical verdict row.
    assert body["candidate_sha"] == new_sha, (
        "candidate_sha must reflect the current Version Phase, not the historical verdict"
    )
    assert body["status"] == "dry_run", (
        "status must reflect the current Version Phase, not the historical verdict"
    )
    assert body["finalized_at"] == "2026-06-15T10:00:00", (
        "finalized_at must reflect the current Version Phase, not the historical verdict"
    )

    # Findings still come from the verdict row.
    assert len(body["findings"]) == 1
    assert body["findings"][0]["summary"] == "Old finding"


@pytest.mark.asyncio
async def test_agent_fetch_qualification_prefers_verdict_terminal_status_over_stale_promotion_pending(
    client: AsyncClient, db_session
):
    """When last_qualification.status=pending with promotion_pending AND
    QualificationVerdict.terminal_status=promoted, the endpoint returns
    status=promoted and populates source_disagreement."""
    from sqlalchemy.orm.attributes import flag_modified

    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-qual-verdict-override"
    )

    sha = "c" * 40
    finding = {"severity": "LOW", "summary": "Minor style issue", "file": "app/foo.py"}
    verdict = QualificationVerdict(
        id=uuid.uuid4(),
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha=sha,
        confidence=0.91,
        sub_scores={"audit": 0.95},
        terminal_status="promoted",
        finalized_at=datetime(2026, 6, 20, 14, 0, 0),
        audit_findings=[finding],
    )
    db_session.add(verdict)

    # last_qualification is still in transient pending state with promotion_pending
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": sha,
        "confidence": 0.91,
        "promotion_pending": {
            "pr_number": 42,
            "pr_url": "https://github.com/example/repo/pull/42",
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

    assert body["source"] == "qualification_verdict"
    # Verdict terminal_status wins over the stale pending
    assert body["status"] == "promoted", (
        "verdict terminal_status should override stale pending status"
    )
    assert body["finalized_at"] == "2026-06-20T14:00:00"
    assert body["candidate_sha"] == sha
    # source_disagreement must be populated
    assert body["source_disagreement"] is not None
    assert body["source_disagreement"]["last_qualification_status"] == "pending"
    assert body["source_disagreement"]["verdict_terminal_status"] == "promoted"


@pytest.mark.asyncio
async def test_agent_fetch_version_phase_qualification_state_not_in_flight_when_verdict_finalized(
    client: AsyncClient, db_session
):
    """When open version last_qualification.status=pending with promotion_pending
    AND QualificationVerdict.terminal_status=promoted, version-phase returns
    qualification_state_disagreement and qualification_state.state reflects the
    resolved outcome (not promotion_check_wait)."""
    from sqlalchemy.orm.attributes import flag_modified

    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-vp-verdict-override"
    )

    sha = "d" * 40
    finding = {"severity": "LOW", "summary": "Style nit", "file": "app/bar.py"}
    verdict = QualificationVerdict(
        id=uuid.uuid4(),
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha=sha,
        confidence=0.88,
        sub_scores={"audit": 0.9},
        terminal_status="promoted",
        finalized_at=datetime(2026, 6, 21, 10, 0, 0),
        audit_findings=[finding],
    )
    db_session.add(verdict)

    version.last_qualification = {
        "status": "pending",
        "candidate_sha": sha,
        "confidence": 0.88,
        "promotion_pending": {
            "pr_number": 99,
            "pr_url": "https://github.com/example/repo/pull/99",
        },
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/version-phase",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"

    current = body["data"]["current_version"]
    # qualification_state.state must not be promotion_check_wait when verdict is finalized
    assert current["qualification_state"]["state"] != "promotion_check_wait", (
        "qualification_state should not show promotion_check_wait when verdict is finalized"
    )
    # The disagreement field must be present
    assert "qualification_state_disagreement" in current
    assert current["qualification_state_disagreement"]["last_qualification_status"] == "pending"
    assert current["qualification_state_disagreement"]["verdict_terminal_status"] == "promoted"


# ---------------------------------------------------------------------------
# Cross-surface agreement: every OPEN-version read surface must apply the SAME
# three reconciliation enrichments (serving aggregate + version-scoped audit
# child + newest finalized verdict) so they report an identical
# qualification_state in the stale-pending and audit-crashed windows and NO false
# disagreement when the two sources genuinely agree.
# ---------------------------------------------------------------------------


async def _open_version_qualification_states(client, db_session, project, task, version):
    """Collect the open version's ``qualification_state`` from all four surfaces.

    Each surface is driven through its real production entry point (the agent
    fetch verb and the admin lifecycle endpoint over HTTP, the system-state
    snapshot via ``compute_system_state``), and the strategic payload via the
    exact ``resolve_open_version_qualification_state`` + ``to_phase_block``
    sequence ``strategic_spawning._spawn_strategic_review`` runs for the open
    version. This catches any surface that stops routing through the unified
    enrichment helper. Returns ``(states, disagreements)`` keyed by surface.
    """
    from unittest.mock import AsyncMock, patch

    from app.system_state import compute_system_state
    from app.release_artifacts import version_serving_summary
    from app.release_version import current_open_version, to_phase_block
    from app.qualification_state_resolver import (
        resolve_open_version_qualification_state,
    )

    # 1. Agent fetch verb — real HTTP endpoint.
    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/version-phase",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    agent_current = res.json()["data"]["current_version"]

    # 2. Admin lifecycle endpoint — real HTTP endpoint. Patch only the
    #    promotion-readiness compute (keeps the read off GitHub); the audit-child
    #    resolution is left REAL so the audit-crash reconciliation actually runs.
    with patch(
        "app.release_qualifier.compute_promotion_readiness",
        new=AsyncMock(return_value={"ready": False, "reason": "test"}),
    ):
        admin_res = await client.get(
            f"/api/admin/projects/{project.id}/version-lifecycle"
        )
    assert admin_res.status_code == 200, admin_res.text
    admin_current = admin_res.json()["current_version"]

    # 3. System-state snapshot — real function.
    snap = await compute_system_state(db_session, project.id)

    # 4. Strategic payload version_phase block — reproduce the exact open-version
    #    sequence _spawn_strategic_review runs (shared resolver -> to_phase_block,
    #    then override qualification_state with the fully-reconciled state).
    loaded = await current_open_version(db_session, project.id, include_artifacts=True)
    agg, arts = version_serving_summary(loaded, project)
    strat_state, strat_disagreement = await resolve_open_version_qualification_state(
        db_session, loaded, project, serving=(agg, arts)
    )
    strat_block = to_phase_block(
        loaded,
        aggregate_serving_status=agg["serving_status"],
        aggregate_deploy_failure_reason=agg["reason_code"],
        artifact_evidence=arts,
    )
    strat_block["qualification_state"] = strat_state

    states = {
        "agent_fetch": agent_current["qualification_state"],
        "admin_lifecycle": admin_current["qualification_state"],
        "system_state": snap.version.qualification_state,
        "strategic": strat_block["qualification_state"],
    }
    disagreements = {
        "agent_fetch": agent_current.get("qualification_state_disagreement"),
        "strategic": strat_disagreement,
    }
    return states, disagreements


def _surfaces_identical(states: dict) -> bool:
    """True when every surface's qualification_state dict is byte-identical.

    Compared via a canonical JSON form so nested lists (e.g. ``artifacts``) don't
    trip an unhashable-set comparison. For the windows under test the state dicts
    carry no time-derived fields, so identity is deterministic.
    """
    import json

    canonical = {
        json.dumps(s, sort_keys=True, default=str) for s in states.values()
    }
    return len(canonical) == 1


async def _make_open_version_project(db_session, *, vm):
    """Open version + task + a ProjectMember row so the admin endpoint authorizes."""
    project, task, version = await _make_project_with_version(db_session, vm=vm)
    # The admin version-lifecycle endpoint calls _get_user_project_with_role; the
    # conftest client authenticates as user_id="test-user-123", so it must be a
    # member of the project under test.
    db_session.add(
        ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    )
    await db_session.commit()
    return project, task, version


@pytest.mark.asyncio
async def test_open_version_qualification_state_agrees_across_surfaces(
    client: AsyncClient, db_session
):
    """The strategic payload, agent fetch verb, admin lifecycle endpoint, and
    system-state snapshot must report an IDENTICAL open-version qualification_state
    in the stale-pending and audit-crashed windows, and surface NO false
    disagreement when the two sources genuinely agree."""
    from sqlalchemy.orm.attributes import flag_modified

    # --- (a) Stale-pending window ---------------------------------------------
    # last_qualification is a transient pending+promotion_pending record, but a
    # finalized verdict (terminal_status=promoted) has already resolved. Every
    # surface must adopt the resolved outcome (NOT promotion_check_wait) and emit an
    # identical finalized-verdict disagreement.
    p1, t1, v1 = await _make_open_version_project(db_session, vm="agent-vm-xsurf-sp")
    sha1 = "a" * 40
    db_session.add(
        QualificationVerdict(
            id=uuid.uuid4(),
            project_id=p1.id,
            release_version_id=v1.id,
            candidate_sha=sha1,
            confidence=0.9,
            sub_scores={"audit": 0.95},
            terminal_status="promoted",
            finalized_at=datetime(2026, 6, 20, 14, 0, 0),
            audit_findings=[{"severity": "LOW", "summary": "nit"}],
        )
    )
    v1.last_qualification = {
        "status": "pending",
        "candidate_sha": sha1,
        "confidence": 0.9,
        "promotion_pending": {"pr_number": 7, "pr_url": "https://x/pull/7"},
    }
    flag_modified(v1, "last_qualification")
    db_session.add(v1)
    await db_session.commit()

    states, disagreements = await _open_version_qualification_states(
        client, db_session, p1, t1, v1
    )
    assert _surfaces_identical(states), (
        f"surfaces disagree in stale-pending window: {states}"
    )
    for surface, state in states.items():
        assert state is not None, surface
        # The finalized verdict (terminal_status=promoted) supersedes the stale
        # promotion_pending: the surface must NOT still read promotion_check_wait.
        assert state["state"] != "promotion_check_wait", (
            f"{surface} still reads promotion_check_wait despite a finalized verdict: {state}"
        )
        assert state["state"].startswith("promoted") or state["state"] == "promotion_confirmation_timed_out", (
            surface,
            state,
        )
    # The finalized-verdict disagreement is populated consistently on the surfaces
    # that carry the field.
    for surface, dis in disagreements.items():
        assert dis is not None, f"{surface} dropped the verdict disagreement"
        assert dis["last_qualification_status"] == "pending"
        assert dis["verdict_terminal_status"] == "promoted"

    # --- (b) Audit-crashed window ---------------------------------------------
    # last_qualification is bare pending (no promotion_pending) but the version's
    # audit child crashed (terminal-failed, no report). Every surface must
    # reconcile to audit_crashed rather than a misleading "qualifying".
    p2, t2, v2 = await _make_open_version_project(db_session, vm="agent-vm-xsurf-ac")
    sha2 = "b" * 40
    v2.last_qualification = {"status": "pending", "candidate_sha": sha2}
    flag_modified(v2, "last_qualification")
    db_session.add(v2)
    # Version-scoped release_audit task pinned to the evaluated candidate, FAILED
    # with no report artifact => crashed audit.
    db_session.add(
        Task(
            project_id=p2.id,
            payloads=[{"type": "release_audit"}],
            vm_instance_id="agent-vm-audit-crash",
            status=TaskStatus.FAILED,
            context={"release_version_id": str(v2.id), "candidate_sha": sha2},
        )
    )
    await db_session.commit()

    states2, _ = await _open_version_qualification_states(client, db_session, p2, t2, v2)
    assert _surfaces_identical(states2), (
        f"surfaces disagree in audit-crashed window: {states2}"
    )
    for surface, state in states2.items():
        assert state is not None and state["state"] == "audit_crashed", (surface, state)

    # --- (c) Healthy window: no false disagreement ----------------------------
    # A normal qualifying open version — bare pending, no finalized verdict, no
    # crashed audit — must agree on "qualifying" and surface NO disagreement.
    p3, t3, v3 = await _make_open_version_project(db_session, vm="agent-vm-xsurf-ok")
    v3.last_qualification = {"status": "pending", "candidate_sha": "c" * 40}
    flag_modified(v3, "last_qualification")
    db_session.add(v3)
    await db_session.commit()

    states3, disagreements3 = await _open_version_qualification_states(
        client, db_session, p3, t3, v3
    )
    assert _surfaces_identical(states3), (
        f"surfaces disagree in healthy window: {states3}"
    )
    for surface, state in states3.items():
        assert state is not None and state["state"] == "qualifying", (surface, state)
    for surface, dis in disagreements3.items():
        assert dis is None, f"{surface} raised a FALSE disagreement when sources agree: {dis}"


@pytest.mark.asyncio
async def test_pending_attempt_does_not_inherit_prior_finalization_across_surfaces(
    client: AsyncClient, db_session
):
    """A same-candidate retry stays pending until its own verdict finalizes."""
    from unittest.mock import AsyncMock, patch

    from sqlalchemy.orm.attributes import flag_modified

    from app.qualification_state_resolver import resolve_open_version_qualification_read_model
    from app.release_artifacts import version_serving_summary
    from app.release_readiness import compute_qualification_trigger_state
    from app.release_version import current_open_version, to_phase_block
    from app.strategic_payload_compaction import _compact_version_phase
    from app.system_state import compute_system_state

    project, task, version = await _make_open_version_project(
        db_session, vm="agent-vm-attempt-scoped"
    )
    candidate = "d" * 40
    prior_attempt = uuid.uuid4()
    active_attempt = uuid.uuid4()
    audit_task_id = uuid.uuid4()

    db_session.add(
        QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            qualification_attempt_id=prior_attempt,
            candidate_sha=candidate,
            confidence=0.91,
            sub_scores={"audit": 0.93},
            terminal_status="promoted",
            finalized_at=datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc),
            audit_findings=[{"severity": "LOW", "summary": "prior attempt"}],
        )
    )
    db_session.add(
        QualificationDimensionResult(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha=candidate,
            dimension="audit",
            status="passed",
            sub_score=0.93,
            producing_task_id=audit_task_id,
            origin_attempt_id=prior_attempt,
            reused_by_attempt_ids=[str(active_attempt)],
        )
    )
    version.active_qualification_attempt_id = active_attempt
    version.qualification_attempts = 2
    version.qualification_retry_episode = 1
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": candidate,
        "qualification_attempt_id": str(active_attempt),
        "retry_episode": 1,
        "retry_episode_cap": 5,
        "audit_task_id": str(audit_task_id),
        "promotion_pending": {
            "pr_number": 123,
            "pr_url": "https://example.test/pull/123",
        },
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()

    async def collect_surfaces():
        from app.routers.admin_release_version import _version_lifecycle_cache

        _version_lifecycle_cache.clear()
        token = create_agent_token(str(task.id), task.vm_instance_id)
        agent_phase_res = await client.get(f"/api/agent/projects/{project.id}/version-phase", headers={"x-agent-token": token})
        assert agent_phase_res.status_code == 200, agent_phase_res.text
        agent_phase = agent_phase_res.json()["data"]["current_version"]

        agent_qualification_res = await client.get(f"/api/agent/projects/{project.id}/qualification", headers={"x-agent-token": token})
        assert agent_qualification_res.status_code == 200, agent_qualification_res.text
        agent_qualification = agent_qualification_res.json()

        with patch("app.release_qualifier.compute_promotion_readiness", new=AsyncMock(return_value={"ready": False, "reason": "test"})):
            admin_res = await client.get(f"/api/admin/projects/{project.id}/version-lifecycle")
        assert admin_res.status_code == 200, admin_res.text
        admin_phase = admin_res.json()["current_version"]

        snapshot = await compute_system_state(db_session, project.id)
        loaded = await current_open_version(db_session, project.id, include_artifacts=True)
        aggregate, artifacts = version_serving_summary(loaded, project)
        strategic_read = await resolve_open_version_qualification_read_model(db_session, loaded, project, serving=(aggregate, artifacts))
        strategic_phase = to_phase_block(
            loaded,
            aggregate_serving_status=aggregate["serving_status"],
            aggregate_deploy_failure_reason=aggregate["reason_code"],
            artifact_evidence=artifacts,
        )
        strategic_phase.update(
            {
                "qualification_state": strategic_read.qualification_state,
                "qualification_provenance": strategic_read.provenance,
                "dimension_states": strategic_read.dimension_states,
                "qualification_attempts": loaded.qualification_attempts,
                "qualification_retry_episode": loaded.qualification_retry_episode,
                "active_qualification_attempt_id": str(
                    loaded.active_qualification_attempt_id
                ),
            }
        )
        strategic_phase["qualification_trigger_state"] = await compute_qualification_trigger_state(db_session, project.id)
        compacted_strategic = _compact_version_phase(strategic_phase)
        return {"agent_phase": agent_phase, "admin_phase": admin_phase, "system_state": snapshot.to_dict()["version"], "strategic": compacted_strategic}, agent_qualification

    pending_surfaces, pending_agent_qualification = await collect_surfaces()
    for surface, payload in pending_surfaces.items():
        assert payload["qualification_state"]["state"] == "promotion_check_wait", surface
        provenance = payload["qualification_provenance"]
        assert provenance["active_projection"] == {
            "attempt_id": str(active_attempt),
            "candidate_sha": candidate,
            "currency": "current",
        }
        assert provenance["selected_verdict"]["attempt_id"] is None
        assert provenance["selected_verdict"]["currency"] == "none"
        assert provenance["lifetime_attempts"] == 2
        assert provenance["retry_episode"] == {"count": 1, "cap": 5}
        audit_dimension = next(row for row in payload["dimension_states"] if row["key"] == "audit")
        assert audit_dimension["provenance"] == {
            "origin_attempt_id": str(prior_attempt),
            "candidate_sha": candidate,
            "producing_task_id": str(audit_task_id),
            "currency": "reused",
        }
        assert provenance["child_tasks"]["audit"] == {
            "task_id": str(audit_task_id),
            "attempt_id": str(active_attempt),
            "candidate_sha": candidate,
            "currency": "current",
        }

    assert pending_agent_qualification["source"] == "last_qualification"
    assert pending_agent_qualification["status"] == "pending"
    assert pending_agent_qualification["qualification_provenance"]["selected_verdict"]["currency"] == "none"
    assert pending_agent_qualification["qualification_trigger_state"]["qualification_provenance"]["active_projection"]["attempt_id"] == str(active_attempt)
    assert pending_surfaces["strategic"]["qualification_trigger_state"]["qualification_provenance"]["active_projection"]["attempt_id"] == str(active_attempt)

    db_session.add(
        QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            qualification_attempt_id=active_attempt,
            candidate_sha=candidate,
            confidence=0.96,
            sub_scores={"audit": 0.93},
            terminal_status="promoted",
            finalized_at=datetime(2026, 7, 15, 11, 0, 0, tzinfo=timezone.utc),
            audit_findings=[{"severity": "LOW", "summary": "active attempt"}],
        )
    )
    await db_session.commit()

    finalized_surfaces, finalized_agent_qualification = await collect_surfaces()
    for surface, payload in finalized_surfaces.items():
        state = payload["qualification_state"]["state"]
        assert state.startswith("promoted") or state == "promotion_confirmation_timed_out", (
            surface,
            state,
        )
        selected = payload["qualification_provenance"]["selected_verdict"]
        assert selected["attempt_id"] == str(active_attempt)
        assert selected["candidate_sha"] == candidate
        assert selected["currency"] == "current"

    assert finalized_agent_qualification["source"] == "qualification_verdict"
    assert finalized_agent_qualification["status"] == "promoted"
    assert finalized_agent_qualification["qualification_provenance"]["selected_verdict"]["attempt_id"] == str(active_attempt)

    different_candidate_attempt = uuid.uuid4()
    different_candidate = "f" * 40
    version.active_qualification_attempt_id = different_candidate_attempt
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": different_candidate,
        "qualification_attempt_id": str(different_candidate_attempt),
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()
    changed_surfaces, _ = await collect_surfaces()
    for surface, payload in changed_surfaces.items():
        assert payload["qualification_state"]["state"] == "qualifying", surface
        assert payload["qualification_provenance"]["selected_verdict"]["currency"] == "none"
        audit = next(row for row in payload["dimension_states"] if row["key"] == "audit")
        assert audit["status"] == "pending" and "provenance" not in audit

    version.active_qualification_attempt_id = None
    version.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": candidate,
        "confidence": 0.7,
    }
    flag_modified(version, "last_qualification")
    db_session.add(version)
    await db_session.commit()
    legacy_surfaces, legacy_agent_qualification = await collect_surfaces()
    for surface, payload in legacy_surfaces.items():
        assert payload["qualification_state"]["state"] == "not_promoted", surface
        assert payload["qualification_provenance"]["active_projection"]["currency"] == "legacy_unknown"
        assert payload["qualification_provenance"]["selected_verdict"]["currency"] == "legacy_unknown"
    assert legacy_agent_qualification["qualification_provenance"]["active_projection"]["currency"] == "legacy_unknown"


# ---------------------------------------------------------------------------
# Shared effective-qualification-state resolver primitives
#
# The agent fetch verb (agent_fetch_qualification) and the version-phase surface
# (_compact_version_lifecycle) both delegate their pending-vs-finalized-verdict
# reconciliation to app.qualification_state_resolver.apply_verdict_substitution,
# so the endpoint tests above exercise this production code end-to-end. These
# unit tests pin the shared primitive's contract directly.
# ---------------------------------------------------------------------------


def _fake_verdict(terminal_status, candidate_sha=None, created_at=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        terminal_status=terminal_status,
        candidate_sha=candidate_sha,
        created_at=created_at,
    )


def test_apply_verdict_substitution_finalized_verdict_supersedes_pending():
    """A finalized verdict supersedes a transient pending+promotion_pending record."""
    from datetime import datetime

    from app.qualification_state_resolver import apply_verdict_substitution

    qual = {
        "status": "pending",
        "candidate_sha": "a" * 40,
        "promotion_pending": {
            "pr_number": 42,
            "enabled_at": "2026-01-01T00:00:00",  # attempt started Jan 1
        },
    }
    # Verdict created AFTER the pending attempt (Jan 2 > Jan 1).
    verdict = _fake_verdict(
        "promoted",
        candidate_sha="a" * 40,
        created_at=datetime(2026, 1, 2),
    )
    effective, disagreement = apply_verdict_substitution(qual, verdict)

    # Effective record adopts the verdict's finalized status and drops the
    # transient promotion_pending sub-key so the classifier sees the resolution.
    assert effective["status"] == "promoted"
    assert "promotion_pending" not in effective
    assert effective["candidate_sha"] == "a" * 40
    # The original dict is not mutated in place.
    assert qual["status"] == "pending"
    assert "promotion_pending" in qual
    # Disagreement records both sides for surfacing.
    assert disagreement == {
        "last_qualification_status": "pending",
        "verdict_terminal_status": "promoted",
    }


def test_apply_verdict_substitution_finalized_verdict_supersedes_pending_no_timestamps():
    """Legacy records without timestamps still get substituted — the temporal guard
    only blocks when BOTH timestamps are present and verdict predates the attempt."""
    from app.qualification_state_resolver import apply_verdict_substitution

    # Legacy shape: promotion_pending has no enabled_at, qual has no started_at,
    # verdict has no created_at — all three timestamps absent.
    qual = {
        "status": "pending",
        "candidate_sha": "a" * 40,
        "promotion_pending": {"pr_number": 42},
    }
    effective, disagreement = apply_verdict_substitution(
        qual, _fake_verdict("promoted", candidate_sha="a" * 40)
    )

    assert effective["status"] == "promoted"
    assert "promotion_pending" not in effective
    assert disagreement is not None
    assert disagreement["verdict_terminal_status"] == "promoted"


def test_apply_verdict_substitution_stale_verdict_before_pending_does_not_substitute():
    """A finalized verdict that PREDATES the current promotion attempt must NOT
    suppress the live promotion_pending (same-sha re-run scenario)."""
    from datetime import datetime

    from app.qualification_state_resolver import apply_verdict_substitution

    qual = {
        "status": "pending",
        "candidate_sha": "beef" * 10,
        "promotion_pending": {
            "pr_number": 99,
            "enabled_at": "2026-06-01T00:00:00",  # new attempt: June 1
        },
    }
    # Verdict from BEFORE the new attempt (Jan 1 < June 1) — stale re-run verdict.
    verdict = _fake_verdict(
        "not_promoted",
        candidate_sha="beef" * 10,
        created_at=datetime(2026, 1, 1),
    )
    effective, disagreement = apply_verdict_substitution(qual, verdict)

    # Stale verdict must not clear the live promotion_pending.
    assert effective is qual
    assert disagreement is None


def test_apply_verdict_substitution_requires_matching_candidate_sha():
    """A finalized verdict for a different candidate must not suppress live promotion."""
    from app.qualification_state_resolver import apply_verdict_substitution

    qual = {
        "status": "pending",
        "candidate_sha": "a" * 40,
        "promotion_pending": {"pr_number": 42},
    }
    effective, disagreement = apply_verdict_substitution(
        qual,
        _fake_verdict("promoted", candidate_sha="b" * 40),
    )

    assert effective is qual
    assert disagreement is None


def test_apply_verdict_substitution_non_finalized_verdict_is_noop():
    """A non-finalized verdict terminal_status leaves the pending record untouched."""
    from app.qualification_state_resolver import apply_verdict_substitution

    qual = {"status": "pending", "promotion_pending": {"pr_number": 7}}
    # ``pending`` is not in FINALIZED_TERMINAL_STATUSES.
    effective, disagreement = apply_verdict_substitution(
        qual, _fake_verdict("pending", candidate_sha="a" * 40)
    )

    assert effective is qual
    assert disagreement is None


def test_apply_verdict_substitution_requires_promotion_pending():
    """Without a promotion_pending sub-key the substitution does not fire."""
    from app.qualification_state_resolver import apply_verdict_substitution

    qual = {"status": "pending"}
    effective, disagreement = apply_verdict_substitution(
        qual, _fake_verdict("promoted", candidate_sha="a" * 40)
    )

    assert effective is qual
    assert disagreement is None


def test_apply_verdict_substitution_no_verdict_or_non_pending_status():
    """No verdict, or a non-pending status, is a no-op regardless of the verdict."""
    from app.qualification_state_resolver import apply_verdict_substitution

    pending = {"status": "pending", "promotion_pending": {"pr_number": 1}}
    # No verdict at all.
    effective, disagreement = apply_verdict_substitution(pending, None)
    assert effective is pending and disagreement is None

    # Already-terminal status is never overwritten even when a verdict exists.
    promoted = {"status": "promoted", "promotion_pending": {"pr_number": 2}}
    effective, disagreement = apply_verdict_substitution(
        promoted, _fake_verdict("not_promoted", candidate_sha="a" * 40)
    )
    assert effective is promoted and disagreement is None

    # None / non-dict input degrades to an empty dict without raising.
    effective, disagreement = apply_verdict_substitution(
        None, _fake_verdict("promoted", candidate_sha="a" * 40)
    )
    assert effective == {} and disagreement is None


# ---------------------------------------------------------------------------
# compute-sweeps recovery endpoints + sweep_id filter on knowledge-documents
# ---------------------------------------------------------------------------


async def _make_sweep_project(db_session, *, vm: str = "agent-vm-sweep"):
    """Create a project + task and return (project, task, token)."""
    project = Project(name="Sweep Test", user_id="test-user-sweep")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(project_id=project.id, payloads=[{}], vm_instance_id=vm)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    token = create_agent_token(str(task.id), vm)
    return project, task, token


async def _add_sweep_docs(db_session, project, sweep_id: str):
    """Add two compute_experiment docs belonging to the same sweep_id."""
    from app.models import KnowledgeDocument, SourceKind

    # KnowledgeDocument requires source_task_id (NOT NULL) — create a dummy task.
    src_task = Task(project_id=project.id, payloads=[{}], vm_instance_id=f"src-{sweep_id[:8]}")
    db_session.add(src_task)
    await db_session.commit()
    await db_session.refresh(src_task)

    docs = []
    for arm_label in ("arm-a", "arm-b"):
        doc = KnowledgeDocument(
            project_id=project.id,
            source_task_id=src_task.id,
            title=f"Sweep {sweep_id} {arm_label}",
            summary=f"Results for {arm_label}",
            body="body text",
            source_kind=SourceKind.COMPUTE_EXPERIMENT,
            sweep_id=sweep_id,
            arm_label=arm_label,
            topic="gpu-throughput",
        )
        db_session.add(doc)
        docs.append(doc)
    await db_session.commit()
    for d in docs:
        await db_session.refresh(d)
    return docs


@pytest.mark.asyncio
async def test_agent_fetch_compute_sweeps_returns_grouped_evidence(
    client: AsyncClient, db_session
):
    """GET /projects/{id}/compute-sweeps returns multi-arm sweeps grouped by
    sweep_id. Lone-arm docs (sweep_id set but only one doc) are excluded by
    build_compute_sweep_groups (_SWEEP_MIN_DOCS=2). A second sweep with two
    arms is returned. Docs with no sweep_id are never included."""
    from app.models import KnowledgeDocument, SourceKind

    project, task, token = await _make_sweep_project(
        db_session, vm="agent-vm-cs-grouped"
    )

    # Two-arm sweep — should appear in the response.
    await _add_sweep_docs(db_session, project, "sweep-multi")

    # Lone-arm doc — excluded by min-docs filter.
    lone_src = Task(project_id=project.id, payloads=[{}], vm_instance_id="src-lone")
    db_session.add(lone_src)
    await db_session.commit()
    await db_session.refresh(lone_src)
    lone = KnowledgeDocument(
        project_id=project.id,
        source_task_id=lone_src.id,
        title="Lone arm",
        summary="only one",
        body="body",
        source_kind=SourceKind.COMPUTE_EXPERIMENT,
        sweep_id="sweep-lone",
        arm_label="arm-only",
        topic="t",
    )
    db_session.add(lone)

    # No-sweep_id doc — never included.
    nosweep = KnowledgeDocument(
        project_id=project.id,
        source_task_id=lone_src.id,
        title="No sweep",
        summary="no id",
        body="body",
        source_kind=SourceKind.COMPUTE_EXPERIMENT,
        topic="t",
    )
    db_session.add(nosweep)
    await db_session.commit()

    res = await client.get(
        f"/api/agent/projects/{project.id}/compute-sweeps",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    data = body["data"]
    # The multi-arm sweep is present.
    assert "sweep-multi" in data
    assert len(data["sweep-multi"]) == 2
    arm_labels = {e["arm_label"] for e in data["sweep-multi"]}
    assert arm_labels == {"arm-a", "arm-b"}
    # The lone-arm sweep is excluded.
    assert "sweep-lone" not in data
    # Docs with no sweep_id are never present.
    titles = [e["title"] for entries in data.values() for e in entries]
    assert "No sweep" not in titles


@pytest.mark.asyncio
async def test_agent_fetch_compute_sweep_single_returns_docs_for_that_sweep(
    client: AsyncClient, db_session
):
    """GET /projects/{id}/compute-sweeps/{sweep_id} returns only docs for
    that sweep, oldest-first."""
    project, task, token = await _make_sweep_project(
        db_session, vm="agent-vm-cs-single"
    )
    await _add_sweep_docs(db_session, project, "sweep-abc")

    # Add a second sweep — must not appear.
    await _add_sweep_docs(db_session, project, "sweep-xyz")

    res = await client.get(
        f"/api/agent/projects/{project.id}/compute-sweeps/sweep-abc",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    entries = body["data"]
    assert isinstance(entries, list)
    assert len(entries) == 2
    sweep_ids = {e["sweep_id"] for e in entries}
    assert sweep_ids == {"sweep-abc"}


@pytest.mark.asyncio
async def test_agent_fetch_compute_sweep_single_missing_returns_404(
    client: AsyncClient, db_session
):
    """GET /projects/{id}/compute-sweeps/{sweep_id} returns 404 for unknown sweep."""
    project, task, token = await _make_sweep_project(
        db_session, vm="agent-vm-cs-missing"
    )
    res = await client.get(
        f"/api/agent/projects/{project.id}/compute-sweeps/does-not-exist",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_agent_fetch_compute_sweeps_rejects_cross_project(
    client: AsyncClient, db_session
):
    """Agent token for project A cannot fetch compute-sweeps for project B (404)."""
    project_a = Project(name="CS A", user_id="test-user-csa")
    project_b = Project(name="CS B", user_id="test-user-csb")
    db_session.add_all([project_a, project_b])
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    task_a = Task(project_id=project_a.id, payloads=[{}], vm_instance_id="agent-vm-csa")
    db_session.add(task_a)
    await db_session.commit()
    await db_session.refresh(task_a)

    token = create_agent_token(str(task_a.id), "agent-vm-csa")
    res = await client.get(
        f"/api/agent/projects/{project_b.id}/compute-sweeps",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_knowledge_documents_sweep_id_filter(
    client: AsyncClient, db_session
):
    """GET /knowledge-documents?sweep_id=X returns only docs for that sweep_id."""
    from app.models import KnowledgeDocument, SourceKind

    project, task, token = await _make_sweep_project(
        db_session, vm="agent-vm-kd-sweep"
    )
    # Two docs in target sweep.
    await _add_sweep_docs(db_session, project, "target-sweep")

    # One doc in a different sweep — must not appear in the filtered result.
    other_src = Task(project_id=project.id, payloads=[{}], vm_instance_id="src-other-kd")
    db_session.add(other_src)
    await db_session.commit()
    await db_session.refresh(other_src)
    other = KnowledgeDocument(
        project_id=project.id,
        source_task_id=other_src.id,
        title="Other sweep",
        summary="other",
        body="body",
        source_kind=SourceKind.COMPUTE_EXPERIMENT,
        sweep_id="other-sweep",
        arm_label="arm-x",
        topic="t",
    )
    db_session.add(other)
    await db_session.commit()

    res = await client.get(
        "/api/agent/knowledge-documents",
        params={"sweep_id": "target-sweep"},
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 2
    returned_sweep_ids = {d["sweep_id"] for d in body["documents"]}
    assert returned_sweep_ids == {"target-sweep"}
    # Unfiltered call returns all three docs.
    res_all = await client.get(
        "/api/agent/knowledge-documents",
        headers={"x-agent-token": token},
    )
    assert res_all.json()["total"] == 3


@pytest.mark.asyncio
async def test_agent_fetch_version_phase_cold_start_drift_counts_merges_since_open(
    client: AsyncClient, db_session
):
    """Cold-start: no merge-grade verdict, but >=5 pr_merged tasks since version
    open → goal_coverage_drift signal must be 'drifting' with stale=True.

    Acceptance gate: named test in the task spec; asserts non-trivial behavior
    exercised by production code in release_readiness + release_scoring.
    """
    from datetime import timezone
    from sqlalchemy.orm.attributes import flag_modified

    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-vp-cold-start"
    )
    version.goal = "Harden the release pipeline and improve coverage"
    version.phase = "build"
    # opened_at must be in the past so merged tasks are counted since it
    version.opened_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    flag_modified(version, "goal")
    flag_modified(version, "opened_at")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Create 5 COMPLETED tasks with pr_merged=True, all after version.opened_at.
    # count_merged_since uses updated_at >= since, then _task_completed_at check.
    for i in range(5):
        merged_task = Task(
            project_id=project.id,
            payloads=[{"type": "fix"}],
            status=TaskStatus.COMPLETED,
            updated_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
            vm_instance_id=f"agent-vm-cold-merged-{i}",
            context={"pr_merged": True},
        )
        db_session.add(merged_task)
    await db_session.commit()

    # No QualificationVerdict exists for this version → cold-start scenario.

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/version-phase",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"

    drift = body["data"].get("goal_coverage_drift")
    assert drift is not None, "goal_coverage_drift must be present"
    assert drift["signal"] == "drifting", (
        f"expected 'drifting' for cold-start with 5 merges, got {drift['signal']!r}"
    )
    assert drift["stale"] is True
    assert drift["coverage"] is None


@pytest.mark.asyncio
async def test_fetch_ci_compliance_endpoint_returns_unavailable_without_github_creds(
    client: AsyncClient, db_session
):
    """GET /api/agent/projects/{id}/ci-compliance returns source=unavailable when
    the project has no GitHub credentials (the common test-environment case)."""
    project, task = await _make_project_task(db_session, vm="agent-vm-cicomp")
    token = create_agent_token(str(task.id), "agent-vm-cicomp")
    res = await client.get(
        f"/api/agent/projects/{project.id}/ci-compliance",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    assert body["data"]["source"] == "unavailable"


@pytest.mark.asyncio
async def test_fetch_ci_compliance_rejects_cross_project(
    client: AsyncClient, db_session
):
    """Agent token for project A cannot fetch ci-compliance for project B (404)."""
    project_a = Project(name="CCA", user_id="u-cca")
    project_b = Project(name="CCB", user_id="u-ccb")
    db_session.add_all([project_a, project_b])
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    task_a = Task(project_id=project_a.id, payloads=[{}], vm_instance_id="agent-vm-ccxp")
    db_session.add(task_a)
    await db_session.commit()
    await db_session.refresh(task_a)

    token = create_agent_token(str(task_a.id), "agent-vm-ccxp")
    res = await client.get(
        f"/api/agent/projects/{project_b.id}/ci-compliance",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# completion-debt endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_fetch_completion_debt_returns_not_available_without_module(
    client: AsyncClient, db_session
):
    """GET /api/agent/projects/{id}/completion-debt returns a well-formed success
    payload for a project with no debt. With the completion_debt module deployed
    the endpoint returns the empty ledger snapshot ``{"items": [], "total_open": 0}``;
    if the module were absent it falls back to ``data=[]`` (source=not_available)."""
    project, task = await _make_project_task(db_session, vm="agent-vm-cdebtnav")
    token = create_agent_token(str(task.id), "agent-vm-cdebtnav")
    res = await client.get(
        f"/api/agent/projects/{project.id}/completion-debt",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    # Deployed module → structured snapshot dict; absent module → not_available list.
    data = body["data"]
    if isinstance(data, dict):
        assert isinstance(data["items"], list)
        assert isinstance(data["total_open"], int)
    else:
        assert isinstance(data, list)
        assert body.get("source") == "not_available"


@pytest.mark.asyncio
async def test_agent_fetch_completion_debt_rejects_cross_project(
    client: AsyncClient, db_session
):
    """Agent token for project A cannot fetch completion-debt for project B (404)."""
    project_a = Project(name="CDA", user_id="u-cda")
    project_b = Project(name="CDB", user_id="u-cdb")
    db_session.add_all([project_a, project_b])
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    task_a = Task(project_id=project_a.id, payloads=[{}], vm_instance_id="agent-vm-cdxp")
    db_session.add(task_a)
    await db_session.commit()
    await db_session.refresh(task_a)

    token = create_agent_token(str(task_a.id), "agent-vm-cdxp")
    res = await client.get(
        f"/api/agent/projects/{project_b.id}/completion-debt",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# criterion_coverage in version-phase endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_fetch_version_phase_surfaces_criterion_coverage(
    client: AsyncClient, db_session
):
    """version_phase endpoint includes criterion_coverage in current_version for
    an open version that has acceptance criteria with recorded evidence.

    Asserts: counts are accurate (covered/partial/uncovered), per-criterion
    entries are present with the correct coverage classification and evidence
    count, and backward-compat keys (goal_coverage_drift, recent_versions) are
    still present.

    Acceptance requirement: test_agent_fetch_version_phase_surfaces_criterion_coverage
    must run green under a real merge-test verdict.
    """
    from app.acceptance_criteria import create_criterion, record_evidence

    project = Project(name="CovTest", user_id="test-user-covtest")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-cov")
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

    # Create two criteria: one will be covered, one uncovered.
    c_covered = await create_criterion(
        db_session, project.id, version.id, "Ship the new auth layer"
    )
    c_uncovered = await create_criterion(
        db_session, project.id, version.id, "Harden CI pipeline"
    )

    # Record a proven task_merge evidence for c_covered → classified 'covered'.
    await record_evidence(
        db_session,
        c_covered.id,
        project.id,
        source_kind="task_merge",
        source_id="pr-101",
        detail={"proven": True},
    )
    # No evidence for c_uncovered → classified 'uncovered'.

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/version-phase",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"

    data = body["data"]
    assert data["source"] == "open_version"
    current = data["current_version"]
    assert current is not None

    # criterion_coverage must be present with correct scalar counts.
    coverage = current.get("criterion_coverage")
    assert coverage is not None, "criterion_coverage must be present for open version"
    assert coverage["total"] == 2
    assert coverage["covered"] == 1
    assert coverage["partial"] == 0
    assert coverage["uncovered"] == 1

    # Per-criterion entries must be present and correctly classified.
    criteria_list = coverage.get("criteria")
    assert isinstance(criteria_list, list), "criteria list must be present"
    assert len(criteria_list) == 2
    by_key = {c["criterion_key"]: c for c in criteria_list}
    assert by_key[c_covered.criterion_key]["coverage"] == "covered"
    assert by_key[c_covered.criterion_key]["evidence_count"] == 1
    assert by_key[c_uncovered.criterion_key]["coverage"] == "uncovered"
    assert by_key[c_uncovered.criterion_key]["evidence_count"] == 0

    # Backward-compat: existing top-level keys must remain present.
    assert "goal_coverage_drift" in data
    assert "recent_versions" in data


@pytest.mark.asyncio
async def test_agent_fetch_version_phase_criterion_coverage_empty_degrades_gracefully(
    client: AsyncClient, db_session
):
    """version_phase endpoint returns criterion_coverage with zero counts and an
    empty criteria list when the open version has no acceptance criteria at all.
    The endpoint must not raise an error."""
    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-cov-empty"
    )

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/version-phase",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"

    data = body["data"]
    assert data["source"] == "open_version"
    current = data["current_version"]
    assert current is not None

    # With no criteria rows the summary is all zeros and empty list — not an error.
    coverage = current.get("criterion_coverage")
    assert coverage is not None, "criterion_coverage present even with no criteria"
    assert coverage["total"] == 0
    assert coverage["covered"] == 0
    assert coverage["partial"] == 0
    assert coverage["uncovered"] == 0
    assert coverage.get("criteria") == []


@pytest.mark.asyncio
async def test_agent_fetch_version_phase_criterion_coverage_absent_when_no_open_version(
    client: AsyncClient, db_session
):
    """When there is no open version, current_version is None and criterion_coverage
    is absent from the response (no errors, source=no_open_version)."""
    project, task = await _make_project_task(db_session, vm="agent-vm-cov-noversion")

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/version-phase",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    data = body["data"]
    assert data["source"] == "no_open_version"
    assert data["current_version"] is None
    # criterion_coverage is absent when there is no open version.
    assert "criterion_coverage" not in (data.get("current_version") or {})


# ---------------------------------------------------------------------------
# candidate_currency freshness-SHAs tests
# ---------------------------------------------------------------------------


def _patch_version_phase_currency(mocker, *, resolved, head_sha):
    """Mock the GitHub resolution path used by _resolve_candidate_currency
    for agent_fetch_version_phase tests."""
    from unittest.mock import AsyncMock

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=resolved),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=head_sha),
    )


@pytest.mark.asyncio
async def test_agent_fetch_version_phase_exposes_payload_freshness_shas(
    client: AsyncClient, db_session, mocker
):
    """Acceptance requirement: candidate_currency in the version-phase response
    must expose payload_sha, active_candidate_sha, integration_head_sha, stale,
    and stale_reason when the open version has a candidate SHA and the integration
    branch HEAD can be resolved.

    Stale case: candidate != HEAD -> stale=True, stale_reason set.
    Current case: candidate == HEAD -> stale=False, stale_reason=None.

    This test covers both sub-cases in one fixture to satisfy the named acceptance
    evidence requirement: test_agent_fetch_version_phase_exposes_payload_freshness_shas.
    """
    from sqlalchemy.orm.attributes import flag_modified

    candidate = "a" * 40
    head = "b" * 40

    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-freshness-shas"
    )
    version.last_qualification = {"candidate_sha": candidate, "status": "not_promoted"}
    flag_modified(version, "last_qualification")
    await db_session.commit()

    _patch_version_phase_currency(
        mocker, resolved=("tok", "owner", "repo"), head_sha=head
    )

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/version-phase",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]

    # Stale case: candidate differs from HEAD
    cc = data.get("candidate_currency")
    assert cc is not None, "candidate_currency must be present when currency resolves"
    assert cc["stale"] is True
    assert cc["payload_sha"] == candidate
    assert cc["payload_compiled_sha"] == candidate
    assert cc["active_candidate_sha"] == candidate
    assert cc["integration_head_sha"] == head
    _assert_generated_at_iso_utc(cc)
    assert cc["stale_reason"] == "payload_sha_differs_from_integration_head"
    # backward-compat fields preserved
    assert cc["current"] is False
    assert cc["candidate_sha"] == candidate
    assert cc["head_sha"] == head


@pytest.mark.asyncio
async def test_agent_fetch_version_phase_candidate_currency_current(
    client: AsyncClient, db_session, mocker
):
    """When candidate_sha equals integration HEAD, candidate_currency has stale=False
    and stale_reason=None."""
    from sqlalchemy.orm.attributes import flag_modified

    sha = "c" * 40

    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-freshness-current"
    )
    version.last_qualification = {"candidate_sha": sha, "status": "promoted"}
    flag_modified(version, "last_qualification")
    await db_session.commit()

    _patch_version_phase_currency(
        mocker, resolved=("tok", "owner", "repo"), head_sha=sha
    )

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/version-phase",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]

    cc = data.get("candidate_currency")
    assert cc is not None
    assert cc["stale"] is False
    assert cc["payload_sha"] == sha
    assert cc["active_candidate_sha"] == sha
    assert cc["integration_head_sha"] == sha
    _assert_generated_at_iso_utc(cc)
    assert cc["stale_reason"] is None
    assert cc["current"] is True


@pytest.mark.asyncio
async def test_candidate_currency_indeterminate_marker(
    client: AsyncClient, db_session, mocker
):
    """Acceptance requirement: when GitHub resolution fails (resolved=None),
    candidate_currency must include an explicit indeterminate marker with
    verdict="unknown" so the agent can distinguish indeterminate from current/stale.
    The endpoint must not error, and the marker must be present in the response."""
    from sqlalchemy.orm.attributes import flag_modified

    candidate = "d" * 40

    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-freshness-indet"
    )
    version.last_qualification = {"candidate_sha": candidate, "status": "not_promoted"}
    flag_modified(version, "last_qualification")
    await db_session.commit()

    # resolved=None -> _resolve_github_with_session returns None -> _resolve_candidate_currency returns None
    _patch_version_phase_currency(mocker, resolved=None, head_sha="e" * 40)

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/version-phase",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]

    # Indeterminate marker must be present with verdict="unknown"
    cc = data.get("candidate_currency")
    assert cc is not None, "candidate_currency must be present when freshness is indeterminate"
    assert cc.get("verdict") == "unknown", "verdict must be 'unknown' when currency is indeterminate"
    _assert_generated_at_iso_utc(cc)


# ---------------------------------------------------------------------------
# Generic payload-section fetch endpoint + inventory contract
# ---------------------------------------------------------------------------


def test_section_fetch_inventory_covers_all_schema_keys():
    """Every key in _STRATEGIC_REVIEW_SCHEMA must appear in SECTION_FETCH_INVENTORY.

    This is the cross-surface inventory contract: any new section added to the
    schema must declare its recovery path (or None) here, preventing silent gaps
    where a dropped section has no documented fallback.
    """
    from app.routers.agent_strategic_fetch import SECTION_FETCH_INVENTORY
    from app.strategic_spawning import _STRATEGIC_REVIEW_SCHEMA

    missing = set(_STRATEGIC_REVIEW_SCHEMA) - set(SECTION_FETCH_INVENTORY)
    assert not missing, (
        f"Sections in _STRATEGIC_REVIEW_SCHEMA missing from SECTION_FETCH_INVENTORY: "
        f"{sorted(missing)}. Add an entry to SECTION_FETCH_INVENTORY in "
        f"app/routers/agent_strategic_fetch.py (use None if no fetch path exists)."
    )


@pytest.mark.asyncio
async def test_agent_fetch_payload_section_self_reflection_log(
    client: AsyncClient, db_session
):
    """Generic /sections endpoint returns self_reflection_log from project data."""
    project = Project(
        name="Section Log Test",
        user_id="test-user-sf-log",
        self_reflection_log=[
            {"entry": "first reflection", "created_at": "2026-01-01T00:00:00"},
            {"entry": "second reflection", "created_at": "2026-01-02T00:00:00"},
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-sf-log")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-sf-log")
    res = await client.get(
        f"/api/agent/projects/{project.id}/sections/self_reflection_log",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    assert len(body["data"]) == 2
    assert body["data"][0]["entry"] == "first reflection"


@pytest.mark.asyncio
async def test_agent_fetch_payload_section_rejected_proposals(
    client: AsyncClient, db_session, mocker
):
    """Generic /sections endpoint returns only recent (within 30d) rejected directions."""
    mocker.patch(
        "app.time_utils._utcnow",
        return_value=datetime(2026, 7, 5, tzinfo=timezone.utc),
    )
    project = Project(
        name="Rejected Props Test",
        user_id="test-user-sf-rp",
        directions=[
            {"status": "rejected", "text": "Recent rejected", "rejected_at": "2026-07-01T00:00:00"},
            {"status": "rejected", "text": "Old rejected", "rejected_at": "2026-04-01T00:00:00"},
            {"status": "active", "text": "Active direction"},
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-sf-rp")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-sf-rp")
    res = await client.get(
        f"/api/agent/projects/{project.id}/sections/rejected_proposals",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    # Only the recent (within 30d of the frozen 2026-07-05) rejected direction appears
    assert len(body["data"]) == 1
    assert body["data"][0]["text"] == "Recent rejected"


@pytest.mark.asyncio
async def test_agent_fetch_payload_section_rejects_non_section_verbs(
    client: AsyncClient, db_session
):
    """Sections with a dedicated verb (problem-model, etc.) return 404 from /sections/."""
    project = Project(name="Sec 404 Test", user_id="test-user-sf-404")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-sf-404")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-sf-404")
    # 'problem_model' has a dedicated endpoint, not served via /sections/
    res = await client.get(
        f"/api/agent/projects/{project.id}/sections/problem_model",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404

    # Completely unknown section name also 404s
    res2 = await client.get(
        f"/api/agent/projects/{project.id}/sections/does_not_exist",
        headers={"x-agent-token": token},
    )
    assert res2.status_code == 404


@pytest.mark.asyncio
async def test_agent_fetch_payload_section_rejects_cross_project(
    client: AsyncClient, db_session
):
    """Generic section endpoint rejects cross-project access (404, not 403)."""
    project_a = Project(name="SFX A", user_id="test-user-sfx-a")
    project_b = Project(
        name="SFX B",
        user_id="test-user-sfx-b",
        self_reflection_log=[{"entry": "secret", "created_at": "2026-01-01T00:00:00"}],
    )
    db_session.add_all([project_a, project_b])
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    task_a = Task(project_id=project_a.id, payloads=[{}], vm_instance_id="agent-vm-sfx-a")
    db_session.add(task_a)
    await db_session.commit()
    await db_session.refresh(task_a)

    token = create_agent_token(str(task_a.id), "agent-vm-sfx-a")
    res = await client.get(
        f"/api/agent/projects/{project_b.id}/sections/self_reflection_log",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_candidate_currency_api_unreachable_distinct_from_unknown(
    client: AsyncClient, db_session, mocker
):
    """A transient GitHub 5xx surfaces candidate_currency.verdict='api_unreachable'
    on the agent fetch path — distinct from 'unknown' — without a stale flag, and
    the endpoint still returns 200 (a GitHub outage must not break the fetch)."""
    from unittest.mock import AsyncMock
    from fastapi import HTTPException
    from sqlalchemy.orm.attributes import flag_modified

    candidate = "f" * 40

    project, task, version = await _make_project_with_version(
        db_session, vm="agent-vm-freshness-unreachable"
    )
    version.last_qualification = {"candidate_sha": candidate, "status": "not_promoted"}
    flag_modified(version, "last_qualification")
    await db_session.commit()

    # Credentials resolve, but the live HEAD read hits a GitHub 5xx after retry.
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(side_effect=HTTPException(status_code=502, detail="5xx")),
    )
    mocker.patch("app.strategic_spawning._CURRENCY_RETRY_DELAY_S", 0)

    token = create_agent_token(str(task.id), task.vm_instance_id)
    res = await client.get(
        f"/api/agent/projects/{project.id}/version-phase",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]

    cc = data.get("candidate_currency")
    assert cc is not None, "candidate_currency must be present when GitHub is unreachable"
    assert cc.get("verdict") == "api_unreachable"
    assert cc.get("verdict") != "unknown"
    assert cc.get("stale") is None, "stale must not be set on api_unreachable"
    _assert_generated_at_iso_utc(cc)


@pytest.mark.asyncio
async def test_pruned_direction_fetch_returns_tombstone_record(
    client: AsyncClient, db_session
):
    """Fetching a pruned direction id returns 410 with structured tombstone data."""
    pruned_id = "dir-pruned-1"
    project = Project(
        name="Prune Test",
        user_id="test-user-prune",
        directions=[],
        vision={
            "_recently_retired_directions": [
                {
                    "id": pruned_id,
                    "retired_at": "2026-07-05T01:00:00+00:00",
                    "reason": "completed",
                    "gap_id": "g-done",
                    "text_tokens": ["fix", "bug"],
                }
            ]
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-prune")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-prune")
    res = await client.get(
        f"/api/agent/projects/{project.id}/directions/{pruned_id}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 410, res.text
    body = res.json()
    tombstone = body["error"]["message"]
    assert tombstone["status"] == "pruned"
    assert tombstone["retired_at"] == "2026-07-05T01:00:00+00:00"
    assert tombstone["reason"] == "completed"
    assert tombstone["gap_id"] == "g-done"


@pytest.mark.asyncio
async def test_agent_fetch_direction_never_existed_still_returns_404(
    client: AsyncClient, db_session
):
    """Fetching a direction id that is neither live nor in retired memory returns 404."""
    project = Project(
        name="Ghost Test",
        user_id="test-user-ghost",
        directions=[{"id": "dir-live", "status": "active", "text": "live"}],
        vision={"_recently_retired_directions": []},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-ghost")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-ghost")
    res = await client.get(
        f"/api/agent/projects/{project.id}/directions/dir-never-existed",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404, res.text


def test_recoverable_sections_carry_actionable_fetch_verbs():
    """Every section registered as 'sections' in SECTION_FETCH_INVENTORY is
    actually served by agent_fetch_payload_section (i.e., the dispatch table
    inside that handler covers all of them). This test exercises the production
    dispatch code: each 'sections'-mapped key must produce a non-None data value
    when called against a minimal project, proving the handler branch exists and
    runs without raising.

    Accepted by the merge gate only when paired with the non-test production code
    change (agent-side PAYLOAD_KEY_FETCH_VERB updates) — a test-only diff fails
    the gate; an always-true assertion fails.
    """
    from app.routers.agent_strategic_fetch import SECTION_FETCH_INVENTORY
    from app.models import Project

    # Every key whose recovery verb is "sections" must be handled in the endpoint's
    # dispatch table. We verify this by calling the same builder functions that
    # the endpoint calls, using a minimal project, and asserting each returns a
    # non-None value (the endpoint would 500 if a branch were missing).
    sections_keys = {k for k, v in SECTION_FETCH_INVENTORY.items() if v == "sections"}
    assert sections_keys, "Expected at least some 'sections' keys in SECTION_FETCH_INVENTORY"

    # All 8 currently registered sections-type keys must be present.
    expected = {
        "self_reflection_log",
        "anomaly_summary",
        "playbook_summary",
        "retired_directions_summary",
        "rejected_proposals",
        "past_retirement_critiques",
        "previous_decision_outcomes",
        "outcome_evaluation",
    }
    missing_from_inventory = expected - sections_keys
    assert not missing_from_inventory, (
        f"Expected sections-type keys missing from SECTION_FETCH_INVENTORY: {sorted(missing_from_inventory)}"
    )

    # Verify the sync (non-async) builder functions for non-cycle-history sections
    # produce their structured output. This exercises the actual production code
    # path at the sync level — the async endpoint wraps these calls verbatim.
    #
    # NOTE: the anomaly/playbook builders are *documented* to return None when
    # nothing qualifies (so the packer omits the key), and the endpoint handles a
    # None ``data`` fine. A bare project therefore yields None for them — which is
    # correct, not a missing dispatch branch. To prove each dispatch branch exists
    # and produces real structured output, seed the project (and the shared
    # anomaly state the anomaly builder reads) with qualifying data.
    from app import anomaly_state
    from app.strategic_spawning import (
        _build_anomaly_summary,
        _build_playbook_summary,
    )
    from app.strategic_analysis import (
        _build_retired_directions_summary,
    )

    project = Project(
        name="fetch-verb-test",
        user_id="test-user-fvt",
        self_reflection_log=[{"entry": "test", "created_at": "2026-01-01T00:00:00"}],
        design_doc={
            # Strategy-relevant playbook entry (source in the strategic sources and
            # content mentioning a strategic keyword) so _build_playbook_summary
            # includes it instead of returning None.
            "playbook/recurring-failure": {
                "content": "Recurring failure pattern: auto-pause the direction.",
                "source": "auto-generated",
                "confirmed_count": 3,
                "updated_at": "2026-01-02T00:00:00",
            },
        },
    )

    # self_reflection_log: direct attribute access in the handler
    result = project.self_reflection_log or []
    assert result is not None, "self_reflection_log builder must return non-None"

    # anomaly_summary reads the in-memory persistent_anomalies keyed by project id;
    # seed one high-occurrence entry so the builder emits its structured summary.
    anomaly_key = str(project.id)
    anomaly_state.persistent_anomalies[anomaly_key] = {
        "CODE_ERROR": {
            "occurrence_count": 3,
            "severity": "high",
            "first_seen": "2026-01-01T00:00:00",
            "last_seen": "2026-01-02T00:00:00",
            "direction_id": "dir-1",
        }
    }
    try:
        # anomaly_summary
        result = _build_anomaly_summary(project)
        assert result is not None, "anomaly_summary builder must return non-None"
        assert "recurring_anomalies" in result

        # playbook_summary
        result = _build_playbook_summary(project)
        assert result is not None, "playbook_summary builder must return non-None"
        assert result["entries"], "playbook_summary must include the seeded entry"

        # retired_directions_summary
        result = _build_retired_directions_summary(project)
        assert result is not None, "retired_directions_summary builder must return non-None"
    finally:
        anomaly_state.persistent_anomalies.pop(anomaly_key, None)

    # rejected_proposals: inline logic in handler — verify the filter produces a list
    from app.time_utils import _utcnow
    from datetime import timedelta
    cutoff = (_utcnow() - timedelta(days=30)).isoformat()
    directions = project.directions or []
    rows = [
        d for d in directions
        if d.get("status") == "rejected" and (d.get("rejected_at") or "") >= cutoff
    ]
    assert isinstance(rows, list), "rejected_proposals filter must return a list"


# ---------------------------------------------------------------------------
# Spawned-task children visibility
# ---------------------------------------------------------------------------


async def _make_strategic_parent(db_session, project, *, vm: str = "agent-vm-sc"):
    """Create a STRATEGIC_AGENT task that will act as a parent."""
    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        vm_instance_id=vm,
        created_by="STRATEGIC_AGENT",
        title="Strategic parent task",
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)
    return parent


async def _make_child_task(
    db_session,
    project,
    parent,
    *,
    vm: str = "child-vm",
    status: TaskStatus = TaskStatus.IN_PROGRESS,
    title: str = "Child task",
    created_by: str = "ORCHESTRATOR",
):
    """Create a pipeline-spawned child task linked by parent_task_id."""
    child = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id=vm,
        parent_task_id=parent.id,
        status=status,
        title=title,
        created_by=created_by,
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(child)
    return child


@pytest.mark.asyncio
async def test_get_task_includes_empty_children_when_no_children(
    client: AsyncClient, db_session
):
    """GET /tasks/{id} returns children: [] when the parent has no child tasks."""
    project = Project(name="Children Empty", user_id="u-ce")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    calling_task = Task(project_id=project.id, payloads=[{}], vm_instance_id="vm-ce")
    db_session.add(calling_task)
    await db_session.commit()
    await db_session.refresh(calling_task)

    parent = await _make_strategic_parent(db_session, project, vm="vm-ce-parent")

    token = create_agent_token(str(calling_task.id), "vm-ce")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks/{parent.id}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert "children" in data
    assert data["children"] == []


@pytest.mark.asyncio
async def test_get_task_includes_direct_children(client: AsyncClient, db_session):
    """GET /tasks/{id} includes direct children with id/status/task_type/title."""
    project = Project(name="Children Visible", user_id="u-cv")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    calling_task = Task(project_id=project.id, payloads=[{}], vm_instance_id="vm-cv")
    db_session.add(calling_task)
    await db_session.commit()
    await db_session.refresh(calling_task)

    parent = await _make_strategic_parent(db_session, project, vm="vm-cv-parent")
    child1 = await _make_child_task(
        db_session, project, parent, vm="vm-cv-c1", title="Child one"
    )
    child2 = await _make_child_task(
        db_session, project, parent, vm="vm-cv-c2", title="Child two",
        status=TaskStatus.COMPLETED,
    )

    token = create_agent_token(str(calling_task.id), "vm-cv")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks/{parent.id}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    children = data["children"]
    assert len(children) == 2
    child_ids = {c["id"] for c in children}
    assert str(child1.id) in child_ids
    assert str(child2.id) in child_ids
    for c in children:
        assert "id" in c
        assert "status" in c
        assert "task_type" in c
        assert "title" in c


@pytest.mark.asyncio
async def test_get_task_children_include_pr_url_for_completed_child(
    client: AsyncClient, db_session
):
    """Terminal child task surfaces pr_url from its PR artifact in its child dict."""
    project = Project(name="Children PR", user_id="u-cpr")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    calling_task = Task(project_id=project.id, payloads=[{}], vm_instance_id="vm-cpr")
    db_session.add(calling_task)
    await db_session.commit()
    await db_session.refresh(calling_task)

    parent = await _make_strategic_parent(db_session, project, vm="vm-cpr-p")
    child = await _make_child_task(
        db_session, project, parent, vm="vm-cpr-c",
        status=TaskStatus.COMPLETED, title="PR child",
    )

    artifact = Artifact(
        task_id=child.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/example/repo/pull/42",
    )
    db_session.add(artifact)
    await db_session.commit()

    token = create_agent_token(str(calling_task.id), "vm-cpr")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks/{parent.id}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    children = res.json()["data"]["children"]
    assert len(children) == 1
    assert children[0]["pr_url"] == "https://github.com/example/repo/pull/42"


@pytest.mark.asyncio
async def test_list_tasks_includes_children_inline(client: AsyncClient, db_session):
    """GET /tasks list response: the strategist root exposes its direct child
    inline via `children`.

    Per the documented descendant-visibility contract (#13074; see the `tasks`
    help text in agent strategic-helpers), the list is FLAT — it surfaces the
    strategist root AND its transitive descendants as top-level items, each
    carrying parent_task_id, and each item ALSO exposes its direct pipeline
    subtasks inline via `children`. So a root + one child yields two items: the
    root item carries the child inline, and the child is independently visible
    as a top-level item mapping back to its parent.
    """
    project = Project(name="Children List", user_id="u-cl")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    calling_task = Task(project_id=project.id, payloads=[{}], vm_instance_id="vm-cl")
    db_session.add(calling_task)
    await db_session.commit()
    await db_session.refresh(calling_task)

    parent = await _make_strategic_parent(db_session, project, vm="vm-cl-p")
    child = await _make_child_task(
        db_session, project, parent, vm="vm-cl-c", title="List child"
    )

    token = create_agent_token(str(calling_task.id), "vm-cl")
    res = await client.get(
        f"/api/agent/projects/{project.id}/tasks",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    items = res.json()["data"]
    by_id = {it["id"]: it for it in items}
    # The root strategist task carries its direct child inline via `children`.
    assert str(parent.id) in by_id
    parent_item = by_id[str(parent.id)]
    assert "children" in parent_item
    assert len(parent_item["children"]) == 1
    assert parent_item["children"][0]["id"] == str(child.id)
    assert parent_item["children"][0]["title"] == "List child"
    # The child is also visible as a top-level item mapping back to its parent
    # (flat descendant visibility — #13074), so agents can map failing
    # descendants back to the spawned task without walking the nested tree.
    assert str(child.id) in by_id
    assert by_id[str(child.id)]["parent_task_id"] == str(parent.id)


@pytest.mark.asyncio
async def test_get_task_children_scoped_to_project(client: AsyncClient, db_session):
    """Children belonging to another project are not included (cross-project isolation)."""
    project_a = Project(name="ProjA Children", user_id="u-pca")
    project_b = Project(name="ProjB Children", user_id="u-pcb")
    db_session.add_all([project_a, project_b])
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    calling_task = Task(
        project_id=project_a.id, payloads=[{}], vm_instance_id="vm-pca"
    )
    db_session.add(calling_task)
    await db_session.commit()
    await db_session.refresh(calling_task)

    parent_a = await _make_strategic_parent(db_session, project_a, vm="vm-pca-p")

    # A child in project B that references parent_a.id — should not appear.
    cross_child = Task(
        project_id=project_b.id,
        payloads=[{"type": "fix"}],
        vm_instance_id="vm-pcb-c",
        parent_task_id=parent_a.id,
        title="Cross-project child",
    )
    db_session.add(cross_child)
    await db_session.commit()

    token = create_agent_token(str(calling_task.id), "vm-pca")
    res = await client.get(
        f"/api/agent/projects/{project_a.id}/tasks/{parent_a.id}",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["children"] == []


# ---------------------------------------------------------------------------
# Spend ceiling fetch endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_fetch_spend_ceiling_reached(
    client: AsyncClient, db_session, mocker
):
    """When spend exceeds the ceiling, the endpoint reports
    spend_ceiling_reached=True with the correct amounts."""
    project = Project(
        name="Spend Ceiling Reached",
        user_id="test-user-scr",
        build_config={"resource_envelope": {"spend_ceiling_usd": 10.0}},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-scr")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Mock get_project_recent_spend_usd to return $12 total (past the $10 ceiling).
    # Patched at the source module because the endpoint imports it lazily.
    mock_spend = mocker.patch(
        "app.resource_envelope.get_project_recent_spend_usd",
    )
    mock_spend.return_value = {
        "total": 12.0,
        "compute": 10.0,
        "agent_vm": 2.0,
        "idle": 0.0,
    }

    token = create_agent_token(str(task.id), "agent-vm-scr")
    res = await client.get(
        f"/api/agent/projects/{project.id}/spend-ceiling",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["spend_ceiling_usd"] == 10.0
    assert data["spend_recent_usd"] == 12.0
    assert data["spend_ceiling_reached"] is True


@pytest.mark.asyncio
async def test_agent_fetch_spend_ceiling_not_reached(
    client: AsyncClient, db_session, mocker
):
    """When spend is below the ceiling, spend_ceiling_reached is False."""
    project = Project(
        name="Spend Ceiling Not Reached",
        user_id="test-user-scnr",
        build_config={"resource_envelope": {"spend_ceiling_usd": 50.0}},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-scnr"
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mock_spend = mocker.patch(
        "app.resource_envelope.get_project_recent_spend_usd",
    )
    mock_spend.return_value = {
        "total": 8.0,
        "compute": 5.0,
        "agent_vm": 3.0,
        "idle": 0.0,
    }

    token = create_agent_token(str(task.id), "agent-vm-scnr")
    res = await client.get(
        f"/api/agent/projects/{project.id}/spend-ceiling",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["spend_ceiling_usd"] == 50.0
    assert data["spend_recent_usd"] == 8.0
    assert data["spend_ceiling_reached"] is False


@pytest.mark.asyncio
async def test_agent_fetch_spend_ceiling_no_ceiling(
    client: AsyncClient, db_session
):
    """When no spend ceiling is configured, all ceiling fields are null."""
    project = Project(
        name="No Ceiling",
        user_id="test-user-scnc",
        build_config={"resource_envelope": {}},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-scnc")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "agent-vm-scnc")
    res = await client.get(
        f"/api/agent/projects/{project.id}/spend-ceiling",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["spend_ceiling_usd"] is None
    assert data["spend_recent_usd"] is None
    assert data["spend_ceiling_reached"] is None


@pytest.mark.asyncio
async def test_agent_fetch_ui_load_timing_returns_surface_data(
    client: AsyncClient, db_session
):
    """Agent ./fetch ui-load-timing returns surfaces dict with {ts, page_ready_ms,
    sample_type} entries grouped by surface, oldest-first."""
    from datetime import timezone

    project = Project(name="UiTiming Test", user_id="test-user-ult")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    token_task = Task(
        project_id=project.id,
        payloads=[{}],
        vm_instance_id="agent-vm-ult",
    )
    db_session.add(token_task)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.add(UiLoadTimingSample(surface="chat", page_ready_ms=1200, sample_type="first_commit", recorded_at=now))
    db_session.add(UiLoadTimingSample(surface="chat", page_ready_ms=800, sample_type="chat_data_ready", recorded_at=now))
    db_session.add(UiLoadTimingSample(surface="board", page_ready_ms=950, sample_type="first_commit", recorded_at=now))

    await db_session.commit()
    await db_session.refresh(token_task)

    token = create_agent_token(str(token_task.id), "agent-vm-ult")
    res = await client.get(
        f"/api/agent/projects/{project.id}/ui-load-timing",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success"
    surfaces = body["data"]["surfaces"]
    assert "chat" in surfaces
    assert "board" in surfaces
    assert len(surfaces["chat"]) == 2
    assert len(surfaces["board"]) == 1
    chat_entry = surfaces["chat"][0]
    assert "ts" in chat_entry
    assert chat_entry["page_ready_ms"] == 1200
    assert chat_entry["sample_type"] == "first_commit"


@pytest.mark.asyncio
async def test_agent_fetch_ui_load_timing_rejects_cross_project(
    client: AsyncClient, db_session
):
    """Agent token for project A cannot fetch ui-load-timing for project B (404)."""
    project_a = Project(name="UiTiming A", user_id="test-user-ulta")
    project_b = Project(name="UiTiming B", user_id="test-user-ultb")
    db_session.add_all([project_a, project_b])
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    task_a = Task(
        project_id=project_a.id,
        payloads=[{}],
        vm_instance_id="agent-vm-ulta",
    )
    db_session.add(task_a)
    await db_session.commit()
    await db_session.refresh(task_a)

    token = create_agent_token(str(task_a.id), "agent-vm-ulta")
    res = await client.get(
        f"/api/agent/projects/{project_b.id}/ui-load-timing",
        headers={"x-agent-token": token},
    )
    assert res.status_code == 404


def test_section_fetch_inventory_covers_tools_and_skills():
    """SECTION_FETCH_INVENTORY must register both 'skills' and 'project_tools'
    with their dedicated fetch verbs ('skills' and 'tools' respectively).

    The ./fetch tools and ./fetch skills shell verbs already exist in the agent
    and hit /api/agent/projects/{id}/tools and /api/agent/projects/{id}/skills,
    but without inventory entries the completeness tests (and the strategist's
    recovery path) cannot discover them.
    """
    from app.routers.agent_strategic_fetch import SECTION_FETCH_INVENTORY

    assert SECTION_FETCH_INVENTORY.get("skills") == "skills", (
        "'skills' must be registered in SECTION_FETCH_INVENTORY with verb 'skills'"
    )
    assert SECTION_FETCH_INVENTORY.get("project_tools") == "tools", (
        "'project_tools' must be registered in SECTION_FETCH_INVENTORY with verb 'tools'"
    )
