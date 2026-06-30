import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import func
from sqlmodel import select

from app.models import (
    ReleaseVersion,
    Alert,
    AlertSeverity,
    Artifact,
    ArtifactType,
    ComputeJob,
    ComputeJobStatus,
    KnowledgeDocument,
    LocalRunner,
    MessageType,
    MessagesLastSeen,
    MessagesNoisePrefs,
    Project,
    ProjectMember,
    ProjectMessage,
    StrategicCycle,
    Task,
    TaskWakeRegistration,
    TaskStatus,
    SourceKind,
    WakeAction,
    WakeDeliveryState,
    _utcnow,
)

TEST_USER_ID = "test-user-123"


def _assert_generated_at_iso_utc(payload: dict) -> None:
    generated_at = payload.get("generated_at")
    assert isinstance(generated_at, str) and generated_at
    parsed = datetime.fromisoformat(generated_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


async def _make_project(session) -> Project:
    project = Project(
        name="Strategic State",
        user_id=TEST_USER_ID,
        directions=[
            {
                "id": "d-low",
                "text": "Low priority cleanup",
                "priority": "low",
                "status": "active",
            },
            {
                "id": "d-high",
                "text": "Unblock the operator workbench",
                "priority": "high",
                "status": "active",
                "recommended_mode": "fundamentals",
            },
        ],
        vision={
            "_strategic_refresh": {
                "requested_at": _utcnow().isoformat(),
                "reason": "operator_message",
                "origin": "operator",
                "count": 2,
                "operator_count": 2,
                "excerpt": "Focus on the persistent operator chat. " + ("x" * 5000),
                "huge": "y" * 10_000,
            },
            "_strategic_refresh_last_fired_at": (
                _utcnow() - timedelta(minutes=20)
            ).isoformat(),
            "_strategic_refresh_last_fired_origin": "operator",
        },
        problem_model={
            "revision": 7,
            "intent": {
                "purpose": "Run the pipeline from a focused operator workbench.",
                "current_focus": "Persistent Strategic interaction",
            },
            "gaps": [
                {
                    "id": "g-1",
                    "name": "Strategic chat reload loses state",
                    "status": "active",
                    "priority": "high",
                    "size": "medium",
                    "active_moves": [],
                    "past_moves": [],
                    "history": [],
                }
            ],
            "capabilities": [
                {"name": "pipeline monitoring", "status": "working"},
                {"name": "local strategic runner", "status": "partial"},
            ],
            "pending_user_confirmation": [
                {
                    "id": "p-1",
                    "type": "major_decision",
                    "proposal": "Keep chat as the main surface.",
                }
            ],
        },
        design_doc={"_meta": {"revision": 3}, "pages": []},
        running_update=(
            "Strategic is consolidating operator chat state.\n\n"
            "Pipeline remains available in the task view."
        ),
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner"))
    await session.commit()
    return project


@pytest.mark.asyncio
async def test_strategic_state_returns_persistent_workbench_projection(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    phase2_task = Task(
        project_id=project.id,
        title="Strategic review",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.IN_PROGRESS,
        strategic_cycle_id="sc-active",
        vm_runtime_cost_banked_db=1.25,
    )
    queued_task = Task(
        project_id=project.id,
        title="Queued follow-up",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        vm_runtime_cost_banked_db=0.75,
    )
    db_session.add(phase2_task)
    db_session.add(queued_task)
    await db_session.commit()
    await db_session.refresh(phase2_task)

    active_cycle = StrategicCycle(
        id="sc-active",
        project_id=project.id,
        outcome="phase2_running",
        phase1_started_at=_utcnow() - timedelta(minutes=10),
        phase1_completed_at=_utcnow() - timedelta(minutes=5),
        phase2_started_at=_utcnow() - timedelta(minutes=5),
        phase2_task_id=phase2_task.id,
    )
    latest_cycle = StrategicCycle(
        id="sc-done",
        project_id=project.id,
        outcome="completed",
        phase1_started_at=_utcnow() - timedelta(hours=1),
        phase1_completed_at=_utcnow() - timedelta(minutes=50),
        phase2_started_at=_utcnow() - timedelta(minutes=50),
        phase2_completed_at=_utcnow() - timedelta(minutes=45),
        summary_text="Opened a new operator workbench direction.",
        directions_created=1,
        tasks_spawned=2,
        cycle_metadata={
            "problem_model_changed": True,
            "running_update_changed": True,
        },
    )
    open_message = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.FEEDBACK,
        from_type="user",
        content="Keep the Strategic surface simple.",
        status="open",
    )
    linked_message = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="system",
        content="A blocker is owned by a direction." + (" extra" * 200),
        status="linked",
        context={
            "_convergence_link": {"kind": "direction", "id": "d-high"},
            "event_type": "blocker_linked",
            "strategic_session": {
                "version": 1,
                "interaction_mode": "continuous",
                "active_surface": "strategic",
                "context_package_id": f"strategic-context:{project.id}",
                "selected_refs": [
                    {
                        "kind": "canvas",
                        "id": f"strategic-canvas:{project.id}",
                        "label": "Raw context map label",
                    },
                    {
                        "kind": "canvas",
                        "id": f"strategic-canvas:{project.id}",
                        "label": "Duplicate context map",
                    },
                ],
                "storage_uri": "gs://secret",
            },
            "huge": "x" * 10_000,
        },
    )
    knowledge_doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=phase2_task.id,
        source_kind=SourceKind.DESIGN.value,
        title="Operator workbench notes",
        summary="The workbench should expose context when requested.",
        body="DO NOT EMBED KNOWLEDGE BODY " + ("body " * 200),
        topic="operator-experience",
        direction_id="d-high",
        tags=["operator", "workbench"],
        source_citations=[],
        forward_quality_verdict={"passed": True},
    )
    artifact = Artifact(
        task_id=phase2_task.id,
        artifact_type=ArtifactType.INVESTIGATION_REPORT,
        storage_uri="gs://blueprint-test/operator-context/report.md",
        logical_key="operator-context-report",
        logs="DO NOT EMBED ARTIFACT LOGS " + ("logs " * 200),
    )
    db_session.add(active_cycle)
    db_session.add(latest_cycle)
    db_session.add(open_message)
    db_session.add(linked_message)
    db_session.add(knowledge_doc)
    db_session.add(artifact)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    data = resp.json()

    assert data["project_id"] == str(project.id)
    assert data["strategic"]["active_cycle"]["id"] == "sc-active"
    assert data["strategic"]["active_cycle"]["has_active_task"] is True
    assert data["strategic"]["active_cycle"]["stale"] is False
    assert data["strategic"]["active_task"]["id"] == str(phase2_task.id)
    assert data["strategic"]["active_phase"] == "strategic_review"
    assert data["strategic"]["latest_cycle"]["id"] == "sc-done"
    assert data["strategic"]["refresh"]["pending"]["origin"] == "operator"
    assert "persistent operator chat" in data["strategic"]["refresh"]["pending"]["excerpt"]
    assert len(data["strategic"]["refresh"]["pending"]["excerpt"]) <= 280
    assert "huge" not in data["strategic"]["refresh"]["pending"]
    assert data["problem_model"]["current_focus"] == "Persistent Strategic interaction"
    assert data["problem_model"]["active_gap_count"] == 1
    assert data["problem_model"]["capability_count"] == 2
    assert data["problem_model"]["pending_confirmation_count"] == 1
    assert data["problem_model"]["top_gap"]["id"] == "g-1"
    assert data["directions"]["active_count"] == 2
    assert data["directions"]["primary"]["id"] == "d-high"
    assert data["running_update"]["excerpt"].startswith("Strategic is consolidating")
    assert data["tasks"]["counts"]["running"] == 1
    assert data["tasks"]["counts"]["queued"] == 1
    assert data["tasks"]["cumulative_estimated_cost_usd"] == 2.0
    assert data["messages"]["counts"] == {"open": 1, "linked": 1}
    assert {m["status"] for m in data["messages"]["items"]} == {"open", "linked"}
    assert data["attention"]["level"] == "clear"
    assert data["attention"]["counts"]["needs_operator"] == 0
    local_runner = data["runtime"]["local_runner"]
    assert local_runner["mode"] == "cloud"
    assert local_runner["status"] == "unavailable"
    assert local_runner["location"] == "unavailable"
    assert local_runner["status_reason"] == "no_runner"
    assert local_runner["fallback_reason"] == "no_runner"
    assert local_runner["strategic_runner_count"] == 0
    assert local_runner["active_task_pinned"] is False
    assert local_runner["active_task_runtime"]["runner_target_id"] is None
    linked_preview = next(
        m for m in data["messages"]["items"] if m["status"] == "linked"
    )
    assert len(linked_preview["content"]) <= 280
    assert linked_preview["context"]["event_type"] == "blocker_linked"
    linked_session = linked_preview["context"]["strategic_session"]
    assert linked_session["interaction_mode"] == "continuous"
    assert linked_session["selected_refs"] == [
        {
            "kind": "canvas",
            "id": f"strategic-canvas:{project.id}",
            "label": "Raw context map label",
        },
    ]
    assert "storage_uri" not in linked_session
    canvas_descriptor = linked_session["selected_ref_descriptors"][0]
    assert canvas_descriptor["label"] == "Strategic context map"
    assert canvas_descriptor["target"] == {
        "surface": "project",
        "section": "problem-model",
    }
    assert data["design"]["revision"] == 3
    package = data["context_package"]
    assert package["id"] == f"strategic-context:{project.id}"
    assert package["title"] == "Persistent Strategic interaction"
    assert len(package["title"]) <= 100
    assert package["status"] == "building"
    assert package["updated_at"]
    assert package["token_count"] is None
    assert package["canvas"]["kind"] == "strategic_context"
    assert {source["kind"]: source["count"] for source in package["sources"]} == {
        "running_update": 1,
        "problem_model": 4,
        "directions": 2,
        "messages": 2,
        "tasks": 2,
        "knowledge": 1,
        "artifacts": 1,
        "design": 1,
    }
    assert package["item_count"] == 14
    assert package["canvas"]["node_count"] >= 8
    assert package["canvas"]["edge_count"] >= 7
    assert package["canvas"]["nodes"]
    assert package["canvas"]["edges"]
    assert any(node["kind"] == "knowledge_document" for node in package["canvas"]["nodes"])
    assert any(node["kind"] == "artifact" for node in package["canvas"]["nodes"])
    assert package["resources"][0]["kind"] == "canvas"
    assert any(resource["kind"] == "knowledge_document" for resource in package["resources"])
    assert any(resource["kind"] == "artifact" for resource in package["resources"])
    assert package["evidence"]["knowledge"]["total"] == 1
    assert package["evidence"]["knowledge"]["items"][0]["id"] == str(knowledge_doc.id)
    assert package["evidence"]["knowledge"]["items"][0]["target"]["document_id"] == str(knowledge_doc.id)
    assert package["evidence"]["artifacts"]["total"] == 1
    assert package["evidence"]["artifacts"]["items"][0]["target"]["task_id"] == str(phase2_task.id)
    assert "storage_uri" not in package["evidence"]["artifacts"]["items"][0]
    assert "gs://blueprint-test" not in str(package)
    assert {entry["id"] for entry in package["drilldowns"]} == {
        "running_update",
        "problem_model",
        "directions",
        "pipeline",
        "runtime",
        "design",
    }
    assert {
        entry["target"]["section"]
        for entry in package["drilldowns"]
        if entry["target"].get("surface") == "project"
    } == {"running-update", "problem-model", "directions", "design-doc"}
    assert "huge" not in package
    assert "A blocker is owned by a direction" not in str(package)
    assert "DO NOT EMBED KNOWLEDGE BODY" not in str(package)
    assert "DO NOT EMBED ARTIFACT LOGS" not in str(package)


@pytest.mark.asyncio
async def test_strategic_state_cost_uses_vm_cost_source_of_truth(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    stale_estimate_only = Task(
        project_id=project.id,
        title="Legacy estimate only",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        estimated_cost_usd=999.0,
    )
    banked_cost = Task(
        project_id=project.id,
        title="Banked retry cost",
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        estimated_cost_usd=999.0,
        vm_runtime_cost_banked_db=1.25,
    )
    archived_cost = Task(
        project_id=project.id,
        title="Archived retry cost",
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        archived=True,
        vm_runtime_cost_banked_db=10.0,
    )
    db_session.add_all([stale_estimate_only, banked_cost, archived_cost])
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")

    assert resp.status_code == 200
    assert resp.json()["tasks"]["cumulative_estimated_cost_usd"] == pytest.approx(1.25)


@pytest.mark.asyncio
async def test_strategic_state_task_focus_uses_full_project_scope(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    base_time = _utcnow() - timedelta(days=2)
    approval_task = Task(
        project_id=project.id,
        title="Older approval still needs judgment",
        payloads=[{"type": "create", "direction_id": "d-approval"}],
        status=TaskStatus.QUEUED,
        requires_approval=True,
        latest_status_message="Waiting for operator approval",
        created_at=base_time,
    )
    blocked_task = Task(
        project_id=project.id,
        title="Older blocked task still matters",
        payloads=[{"type": "research", "direction_id": "d-blocked"}],
        status=TaskStatus.BLOCKED,
        blocker_kind="missing_capability",
        blocked_reason="Needs local runner registration",
        created_at=base_time + timedelta(minutes=1),
    )
    old_queued_task = Task(
        project_id=project.id,
        title="Older queued backlog still matters",
        context={"direction_id": "d-queued-context"},
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        created_at=base_time + timedelta(minutes=2),
    )
    recently_completed_task = Task(
        project_id=project.id,
        title="Recently completed old task",
        payloads=[{"type": "create", "direction_id": "d-done"}],
        status=TaskStatus.COMPLETED,
        created_at=base_time,
        completed_at=base_time + timedelta(days=2),
    )
    older_completed_task = Task(
        project_id=project.id,
        title="Older completion from newer task",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        created_at=base_time + timedelta(days=1),
        completed_at=base_time + timedelta(days=1, hours=1),
    )
    newer_routine_tasks = [
        Task(
            project_id=project.id,
            title=f"Newer routine task {index:03d}",
            payloads=[{"type": "create"}],
            status=TaskStatus.QUEUED,
            created_at=base_time + timedelta(hours=1, minutes=index),
        )
        for index in range(220)
    ]
    db_session.add_all([
        approval_task,
        blocked_task,
        old_queued_task,
        recently_completed_task,
        older_completed_task,
        *newer_routine_tasks,
    ])
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")

    assert resp.status_code == 200, resp.text
    focus = resp.json()["tasks"]["focus"]
    assert focus["version"] == 1
    assert focus["bucket_limit"] == 8
    assert focus["has_more"] is True

    judgment = focus["buckets"]["needs_judgment"]
    assert judgment["total"] == 1
    assert judgment["has_more"] is False
    approval_row = judgment["items"][0]
    assert approval_row["id"] == str(approval_task.id)
    assert approval_row["direction_id"] == "d-approval"
    assert approval_row["requires_approval"] is True
    assert "approval" in approval_row["reasons"]

    friction = focus["buckets"]["friction"]
    assert friction["total"] == 1
    blocked_row = friction["items"][0]
    assert blocked_row["id"] == str(blocked_task.id)
    assert blocked_row["direction_id"] == "d-blocked"
    assert blocked_row["blocked_reason"] == "Needs local runner registration"
    assert "blocked" in blocked_row["reasons"]

    queued = focus["buckets"]["queued"]
    assert queued["total"] == 221
    assert queued["has_more"] is True
    assert queued["items"][0]["id"] == str(old_queued_task.id)
    assert queued["items"][0]["direction_id"] == "d-queued-context"
    assert all(row["id"] != str(approval_task.id) for row in queued["items"])

    completed = focus["buckets"]["recent_completed"]
    assert completed["total"] == 2
    assert completed["items"][0]["id"] == str(recently_completed_task.id)
    assert completed["items"][0]["completed_at"] == recently_completed_task.completed_at.isoformat()
    assert completed["items"][0]["direction_id"] == "d-done"


@pytest.mark.asyncio
async def test_strategic_state_reports_active_local_strategic_runner(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    runner = LocalRunner(
        project_id=project.id,
        token_hash="a" * 64,
        hostname="desk-mini",
        status="active",
        last_seen_at=_utcnow(),
        capabilities={
            "task_types": ["design_doc_update", "strategic_review"],
            "has_gpu": False,
            "max_concurrent": 1,
            "runner_preset": "strategic_local",
            "workload": "strategic",
            "agent_available": True,
            "active_task_ids": [],
        },
    )
    db_session.add(runner)
    await db_session.commit()
    await db_session.refresh(runner)

    phase2_task = Task(
        project_id=project.id,
        title="Strategic review on local runner",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.IN_PROGRESS,
        strategic_cycle_id="sc-local",
        vm_instance_id=f"runner:{runner.id}",
        context={
            "runner_target_id": str(runner.id),
            "llm_provider": "openai",
            "llm_model": "gpt-5.4",
            "llm_account_id": "acct-local",
        },
    )
    db_session.add(phase2_task)
    await db_session.commit()
    await db_session.refresh(phase2_task)

    active_cycle = StrategicCycle(
        id="sc-local",
        project_id=project.id,
        outcome="phase2_running",
        phase1_started_at=_utcnow() - timedelta(minutes=10),
        phase1_completed_at=_utcnow() - timedelta(minutes=5),
        phase2_started_at=_utcnow() - timedelta(minutes=5),
        phase2_task_id=phase2_task.id,
    )
    db_session.add(active_cycle)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    data = resp.json()
    local_runner = data["runtime"]["local_runner"]

    assert local_runner["mode"] == "local"
    assert local_runner["status"] == "working"
    assert local_runner["location"] == "locally"
    assert local_runner["status_reason"] is None
    assert local_runner["strategic_runner_count"] == 1
    assert local_runner["live_runner_count"] == 1
    assert local_runner["eligible_live_count"] == 1
    assert local_runner["fallback_reason"] is None
    assert local_runner["active_runner_id"] == str(runner.id)
    assert local_runner["active_task_pinned"] is True
    assert local_runner["active_task_runtime"] == {
        "provider": "openai",
        "model": "gpt-5.4",
        "account_id": "acct-local",
        "vm_instance_id": f"runner:{runner.id}",
        "runner_target_id": str(runner.id),
    }
    assert local_runner["liveness_window_seconds"] == 300
    assert data["work_session"]["status"] == "working"
    assert data["work_session"]["location"] == "locally"


@pytest.mark.asyncio
async def test_strategic_state_reports_offline_strategic_runner(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    db_session.add(
        LocalRunner(
            project_id=project.id,
            token_hash="b" * 64,
            hostname="desk-mini",
            status="active",
            last_seen_at=_utcnow() - timedelta(minutes=10),
            capabilities={
                "task_types": ["design_doc_update", "strategic_review"],
                "has_gpu": False,
                "max_concurrent": 1,
                "runner_preset": "strategic_local",
                "workload": "strategic",
                "agent_available": True,
            },
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    local_runner = resp.json()["runtime"]["local_runner"]

    assert local_runner["mode"] == "pending_local"
    assert local_runner["status"] == "unavailable"
    assert local_runner["location"] == "locally"
    assert local_runner["status_reason"] == "runner_offline"
    assert local_runner["fallback_reason"] == "runner_offline"
    assert local_runner["strategic_runner_count"] == 1
    assert local_runner["live_runner_count"] == 0
    assert local_runner["eligible_live_count"] == 0
    assert local_runner["stale_runner_count"] == 1


@pytest.mark.asyncio
async def test_strategic_state_reports_live_local_runner_as_idle(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    db_session.add(
        LocalRunner(
            project_id=project.id,
            token_hash="f" * 64,
            hostname="idle-desk",
            status="active",
            last_seen_at=_utcnow(),
            capabilities={
                "task_types": ["design_doc_update", "strategic_review"],
                "has_gpu": False,
                "max_concurrent": 1,
                "runner_preset": "strategic_local",
                "workload": "strategic",
                "agent_available": True,
                "active_task_ids": [],
            },
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    data = resp.json()
    local_runner = data["runtime"]["local_runner"]

    assert local_runner["mode"] == "local_ready"
    assert local_runner["status"] == "idle"
    assert local_runner["location"] == "locally"
    assert local_runner["status_reason"] is None
    assert local_runner["eligible_live_count"] == 1
    assert resp.json()["work_session"]["status"] == "idle"


@pytest.mark.asyncio
async def test_strategic_state_reports_active_cloud_strategic_task_as_working(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    phase2_task = Task(
        project_id=project.id,
        title="Strategic review on cloud VM",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.IN_PROGRESS,
        strategic_cycle_id="sc-cloud",
        vm_instance_id="gce-strategic-1",
    )
    db_session.add(phase2_task)
    await db_session.commit()
    await db_session.refresh(phase2_task)

    active_cycle = StrategicCycle(
        id="sc-cloud",
        project_id=project.id,
        outcome="phase2_running",
        phase1_started_at=_utcnow() - timedelta(minutes=10),
        phase1_completed_at=_utcnow() - timedelta(minutes=5),
        phase2_started_at=_utcnow() - timedelta(minutes=5),
        phase2_task_id=phase2_task.id,
    )
    db_session.add(active_cycle)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    data = resp.json()
    local_runner = data["runtime"]["local_runner"]

    assert local_runner["mode"] == "cloud"
    assert local_runner["status"] == "working"
    assert local_runner["location"] == "cloud"
    assert local_runner["status_reason"] == "cloud_dispatch"
    assert local_runner["fallback_reason"] == "cloud_dispatch"
    assert local_runner["active_task_runtime"]["vm_instance_id"] == "gce-strategic-1"
    assert data["work_session"]["status"] == "working"
    assert data["work_session"]["location"] == "cloud"


@pytest.mark.asyncio
async def test_strategic_state_marks_active_cycle_with_cancelled_task_as_stale(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    cancelled_task = Task(
        project_id=project.id,
        title="Cancelled Strategic review",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.CANCELLED,
    )
    db_session.add(cancelled_task)
    await db_session.commit()
    await db_session.refresh(cancelled_task)
    stale_cycle = StrategicCycle(
        id="sc-stale",
        project_id=project.id,
        outcome="phase2_running",
        phase1_started_at=_utcnow() - timedelta(hours=2),
        phase2_task_id=cancelled_task.id,
    )
    db_session.add(stale_cycle)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    active = resp.json()["strategic"]["active_cycle"]
    assert active["id"] == "sc-stale"
    assert active["has_active_task"] is False
    assert active["stale"] is True
    assert resp.json()["strategic"]["active_task"] is None
    assert resp.json()["context_package"]["status"] == "stale"
    metrics = {
        entry["id"]: entry for entry in resp.json()["work_session"]["curated_metrics"]
    }
    assert metrics["context.stale"]["value"] == 1
    assert metrics["context.stale"]["tone"] == "warning"
    assert metrics["context.stale"]["target"] == {
        "surface": "project",
        "section": "problem-model",
    }


@pytest.mark.asyncio
async def test_strategic_state_does_not_return_failed_task_as_active(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    failed_task = Task(
        project_id=project.id,
        title="Failed Strategic review",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.FAILED,
        strategic_cycle_id="sc-failed-task",
    )
    db_session.add(failed_task)
    await db_session.commit()
    await db_session.refresh(failed_task)
    stale_cycle = StrategicCycle(
        id="sc-failed-task",
        project_id=project.id,
        outcome="phase2_running",
        phase1_started_at=_utcnow() - timedelta(hours=1),
        phase2_task_id=failed_task.id,
    )
    db_session.add(stale_cycle)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    strategic = resp.json()["strategic"]
    assert strategic["active_cycle"]["id"] == "sc-failed-task"
    assert strategic["active_cycle"]["has_active_task"] is False
    assert strategic["active_cycle"]["stale"] is True
    assert strategic["active_task"] is None
    assert strategic["active_phase"] is None


@pytest.mark.asyncio
async def test_strategic_state_does_not_mark_partial_strategic_runner_ready(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    db_session.add(
        LocalRunner(
            project_id=project.id,
            token_hash="c" * 64,
            hostname="phase2-only",
            status="active",
            last_seen_at=_utcnow(),
            capabilities={
                "task_types": ["strategic_review"],
                "has_gpu": False,
                "max_concurrent": 1,
                "runner_preset": "strategic_local",
                "workload": "strategic",
                "agent_available": True,
            },
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    data = resp.json()
    local_runner = data["runtime"]["local_runner"]

    assert local_runner["mode"] == "pending_local"
    assert local_runner["status"] == "unavailable"
    assert local_runner["location"] == "locally"
    assert local_runner["status_reason"] == "capability_mismatch"
    assert local_runner["fallback_reason"] == "capability_mismatch"
    assert local_runner["strategic_runner_count"] == 1
    assert local_runner["live_runner_count"] == 1
    assert local_runner["eligible_live_count"] == 0
    assert data["work_session"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_strategic_state_reports_stale_pinned_runner_as_pending(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    runner = LocalRunner(
        project_id=project.id,
        token_hash="d" * 64,
        hostname="stale-desk",
        status="active",
        last_seen_at=_utcnow() - timedelta(minutes=10),
        capabilities={
            "task_types": ["design_doc_update", "strategic_review"],
            "has_gpu": False,
            "max_concurrent": 1,
            "runner_preset": "strategic_local",
            "workload": "strategic",
            "agent_available": True,
        },
    )
    db_session.add(runner)
    await db_session.commit()
    await db_session.refresh(runner)

    phase1_task = Task(
        project_id=project.id,
        title="Design doc update waiting on stale runner",
        payloads=[{"type": "design_doc_update"}],
        status=TaskStatus.PROVISIONING,
        strategic_cycle_id="sc-stale-runner",
        vm_instance_id=f"runner:{runner.id}",
        context={"runner_target_id": str(runner.id)},
    )
    db_session.add(phase1_task)
    await db_session.commit()
    await db_session.refresh(phase1_task)

    active_cycle = StrategicCycle(
        id="sc-stale-runner",
        project_id=project.id,
        outcome="phase1_running",
        phase1_started_at=_utcnow() - timedelta(minutes=10),
        phase1_task_id=phase1_task.id,
    )
    db_session.add(active_cycle)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    data = resp.json()
    local_runner = data["runtime"]["local_runner"]

    assert local_runner["mode"] == "pending_local"
    assert local_runner["status"] == "unavailable"
    assert local_runner["location"] == "locally"
    assert local_runner["status_reason"] == "runner_offline"
    assert local_runner["fallback_reason"] == "runner_offline"
    assert local_runner["active_runner_id"] == str(runner.id)
    assert local_runner["active_task_pinned"] is True
    assert local_runner["live_runner_count"] == 0
    assert local_runner["eligible_live_count"] == 0
    assert data["work_session"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_strategic_state_reports_live_pinned_task_waiting_for_runner_claim(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    runner = LocalRunner(
        project_id=project.id,
        token_hash="e" * 64,
        hostname="live-desk",
        status="active",
        last_seen_at=_utcnow(),
        capabilities={
            "task_types": ["design_doc_update", "strategic_review"],
            "has_gpu": False,
            "max_concurrent": 1,
            "runner_preset": "strategic_local",
            "workload": "strategic",
            "agent_available": True,
        },
    )
    db_session.add(runner)
    await db_session.commit()
    await db_session.refresh(runner)

    phase1_task = Task(
        project_id=project.id,
        title="Design doc update pinned but not claimed",
        payloads=[{"type": "design_doc_update"}],
        status=TaskStatus.PROVISIONING,
        strategic_cycle_id="sc-claim-pending",
        vm_instance_id=f"runner:{runner.id}",
        context={"runner_target_id": str(runner.id)},
    )
    db_session.add(phase1_task)
    await db_session.commit()
    await db_session.refresh(phase1_task)

    active_cycle = StrategicCycle(
        id="sc-claim-pending",
        project_id=project.id,
        outcome="phase1_running",
        phase1_started_at=_utcnow() - timedelta(minutes=2),
        phase1_task_id=phase1_task.id,
    )
    db_session.add(active_cycle)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    data = resp.json()
    local_runner = data["runtime"]["local_runner"]

    assert local_runner["mode"] == "pending_local"
    assert local_runner["status"] == "unavailable"
    assert local_runner["location"] == "locally"
    assert local_runner["status_reason"] == "runner_claim_pending"
    assert local_runner["fallback_reason"] == "runner_claim_pending"
    assert local_runner["active_runner_id"] == str(runner.id)
    assert local_runner["active_task_pinned"] is True
    assert local_runner["live_runner_count"] == 1
    assert local_runner["eligible_live_count"] == 1
    assert data["work_session"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_strategic_state_marks_active_cycle_without_live_task_as_stale(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    cancelled_task = Task(
        project_id=project.id,
        title="Cancelled Strategic review",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.CANCELLED,
    )
    db_session.add(cancelled_task)
    await db_session.commit()
    await db_session.refresh(cancelled_task)
    stale_cycle = StrategicCycle(
        id="sc-stale",
        project_id=project.id,
        outcome="phase2_running",
        phase1_started_at=_utcnow() - timedelta(hours=2),
        phase2_task_id=cancelled_task.id,
    )
    db_session.add(stale_cycle)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    active = resp.json()["strategic"]["active_cycle"]
    assert active["id"] == "sc-stale"
    assert active["has_active_task"] is False
    assert active["stale"] is True
    assert resp.json()["strategic"]["active_task"] is None
    assert resp.json()["context_package"]["status"] == "stale"


@pytest.mark.asyncio
async def test_strategic_state_includes_operator_workbench_state(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    last_interaction_at = _utcnow().isoformat()
    context_task = Task(
        project_id=project.id,
        title="Review active wave",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.IN_PROGRESS,
        latest_status_message="Reviewing context descriptors.",
    )
    db_session.add(context_task)
    await db_session.commit()
    await db_session.refresh(context_task)
    context_cycle = StrategicCycle(
        id="sc-workbench-context",
        project_id=project.id,
        outcome="phase2_running",
        phase1_started_at=_utcnow() - timedelta(minutes=15),
        phase2_task_id=context_task.id,
    )
    knowledge_doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=context_task.id,
        source_kind=SourceKind.DESIGN.value,
        title="Operator context notes",
        summary="Context package evidence should rehydrate kept refs.",
        body="body",
        topic="operator-context",
        direction_id="d-workbench",
        tags=[],
        source_citations=[],
        forward_quality_verdict={"passed": True},
    )
    artifact = Artifact(
        task_id=context_task.id,
        artifact_type=ArtifactType.INVESTIGATION_REPORT,
        storage_uri="gs://blueprint-test/context-report.md",
        logical_key="operator-context-report",
    )
    db_session.add(context_cycle)
    db_session.add(knowledge_doc)
    db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(knowledge_doc)
    await db_session.refresh(artifact)
    db_session.add(
        MessagesNoisePrefs(
            user_id=TEST_USER_ID,
            project_id=project.id,
            prefs={
                "_strategic_workbench": {
                    "version": 1,
                    "interaction_mode": "check_in",
                    "active_surface": "pipeline",
                    "selected_refs": [
                        {
                            "kind": "task",
                            "id": str(context_task.id),
                            "label": "Stale task label",
                        },
                        {
                            "kind": "knowledge_document",
                            "id": f"knowledge:{knowledge_doc.id}",
                        },
                        {
                            "kind": "artifact",
                            "id": f"artifact:{artifact.id}",
                        },
                        {
                            "kind": "task",
                            "id": "task-not-in-package",
                            "label": "Missing task",
                        },
                        {
                            "kind": "canvas",
                            "id": f"strategic-canvas:{project.id}",
                        },
                        {
                            "kind": "canvas",
                            "id": f"strategic-canvas:{project.id}",
                            "label": "Duplicate canvas",
                        },
                    ],
                    "session_started_at": last_interaction_at,
                    "last_interaction_at": last_interaction_at,
                },
                "cycle_summary": "routine",
            },
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    workbench = resp.json()["workbench"]

    assert workbench["interaction_mode"] == "check_in"
    assert workbench["active_surface"] == "pipeline"
    assert workbench["selected_refs"] == [
        {"kind": "task", "id": str(context_task.id), "label": "Stale task label"},
        {"kind": "knowledge_document", "id": f"knowledge:{knowledge_doc.id}"},
        {"kind": "artifact", "id": f"artifact:{artifact.id}"},
        {"kind": "task", "id": "task-not-in-package", "label": "Missing task"},
        {"kind": "canvas", "id": f"strategic-canvas:{project.id}"},
    ]
    assert "cycle_summary" not in workbench

    work_session = resp.json()["work_session"]
    assert work_session["version"] == 1
    assert work_session["mode"] == "check_in"
    assert work_session["active_surface"] == "pipeline"
    assert work_session["context_package_id"] == f"strategic-context:{project.id}"
    assert work_session["selected_refs"] == [
        {"kind": "task", "id": str(context_task.id), "label": "Stale task label"},
        {"kind": "knowledge_document", "id": f"knowledge:{knowledge_doc.id}"},
        {"kind": "artifact", "id": f"artifact:{artifact.id}"},
        {"kind": "task", "id": "task-not-in-package", "label": "Missing task"},
        {"kind": "canvas", "id": f"strategic-canvas:{project.id}"},
    ]
    descriptors = {
        (entry["kind"], entry["id"]): entry
        for entry in work_session["selected_ref_descriptors"]
    }
    task_descriptor = descriptors[("task", str(context_task.id))]
    assert task_descriptor["label"] == "Review active wave"
    assert task_descriptor["summary"] == "Reviewing context descriptors."
    assert task_descriptor["status"] == "IN_PROGRESS"
    assert task_descriptor["target"] == {
        "surface": "task",
        "task_id": str(context_task.id),
    }
    knowledge_descriptor = descriptors[
        ("knowledge_document", f"knowledge:{knowledge_doc.id}")
    ]
    assert knowledge_descriptor["label"] == "Operator context notes"
    assert knowledge_descriptor["summary"] == "Context package evidence should rehydrate kept refs."
    assert knowledge_descriptor["status"] == SourceKind.DESIGN.value
    assert knowledge_descriptor["target"]["surface"] == "knowledge"
    assert knowledge_descriptor["target"]["document_id"] == str(knowledge_doc.id)
    artifact_descriptor = descriptors[("artifact", f"artifact:{artifact.id}")]
    assert artifact_descriptor["label"] == "operator-context-report"
    assert artifact_descriptor["summary"] == "Review active wave"
    assert artifact_descriptor["status"] == ArtifactType.INVESTIGATION_REPORT.value
    assert artifact_descriptor["target"] == {
        "surface": "task",
        "task_id": str(context_task.id),
        "artifact_id": str(artifact.id),
    }
    stale_task_descriptor = descriptors[("task", "task-not-in-package")]
    assert stale_task_descriptor["label"] == "Missing task"
    assert "target" not in stale_task_descriptor
    canvas_descriptor = descriptors[("canvas", f"strategic-canvas:{project.id}")]
    assert canvas_descriptor["label"] == "Strategic context map"
    assert canvas_descriptor["target"] == {
        "surface": "project",
        "section": "problem-model",
    }
    metrics = {entry["id"]: entry for entry in work_session["curated_metrics"]}
    assert metrics["messages.open"]["label"] == "open messages"
    assert metrics["tasks.running"]["target"] == {"surface": "pipeline"}
    assert metrics["context.items"]["value"] == resp.json()["context_package"]["item_count"]


@pytest.mark.asyncio
async def test_strategic_session_endpoint_resumes_persisted_state_and_outputs(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    last_interaction_at = _utcnow().isoformat()
    long_operator_content = "What should I watch next? " + ("x" * 500)
    db_session.add(
        MessagesNoisePrefs(
            user_id=TEST_USER_ID,
            project_id=project.id,
            prefs={
                "_strategic_workbench": {
                    "version": 1,
                    "interaction_mode": "continuous",
                    "active_surface": "runtime",
                    "selected_refs": [
                        {
                            "kind": "canvas",
                            "id": f"strategic-canvas:{project.id}",
                            "label": "Context map",
                        }
                    ],
                    "session_started_at": last_interaction_at,
                    "last_interaction_at": last_interaction_at,
                }
            },
        )
    )
    user_message = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="user",
        content=long_operator_content,
        status="resolved",
    )
    strategic_message = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="strategic",
        content="Watch the local Strategic runner registration path.",
        status="open",
        context={"event_type": "strategic_reply"},
    )
    latest_cycle = StrategicCycle(
        id="sc-session",
        project_id=project.id,
        outcome="completed",
        phase1_started_at=_utcnow() - timedelta(minutes=30),
        phase1_completed_at=_utcnow() - timedelta(minutes=25),
        phase2_started_at=_utcnow() - timedelta(minutes=25),
        phase2_completed_at=_utcnow() - timedelta(minutes=20),
        summary_text="Strategic refreshed the local operator session.",
    )
    db_session.add_all([user_message, strategic_message, latest_cycle])
    await db_session.commit()

    first = await client.get(f"/api/admin/projects/{project.id}/strategic-session")
    second = await client.get(f"/api/admin/projects/{project.id}/strategic-session")
    assert first.status_code == 200
    assert second.status_code == 200
    data = first.json()
    repeat = second.json()

    assert data["status"] == "unavailable"
    assert data["location"] == "unavailable"
    assert data["status_reason"] == "no_runner"
    assert data["session"]["mode"] == "continuous"
    assert data["session"]["active_surface"] == "runtime"
    assert data["session"]["selected_refs"] == [
        {
            "kind": "canvas",
            "id": f"strategic-canvas:{project.id}",
            "label": "Context map",
        }
    ]
    assert data["session"]["id"] == repeat["session"]["id"]
    assert data["conversation"]["limit"] == 20
    assert data["conversation"]["has_more"] is False
    assert data["conversation"]["order"] == "oldest_first"
    conversation_ids = {item["id"] for item in data["conversation"]["items"]}
    assert str(user_message.id) in conversation_ids
    assert str(strategic_message.id) in conversation_ids
    conversation_by_id = {item["id"]: item for item in data["conversation"]["items"]}
    assert conversation_by_id[str(user_message.id)]["content"] == long_operator_content
    output_contents = [item["content"] for item in data["recent_outputs"]]
    assert "Strategic refreshed the local operator session." in output_contents
    assert "Watch the local Strategic runner registration path." in output_contents
    assert "What should I watch next?" not in output_contents


@pytest.mark.asyncio
async def test_strategic_session_conversation_marks_more_history(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    base_time = _utcnow() - timedelta(minutes=30)
    messages = [
        ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="user" if index % 2 == 0 else "strategic",
            content=f"session row {index:02d}",
            created_at=base_time + timedelta(minutes=index),
        )
        for index in range(21)
    ]
    db_session.add_all(messages)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-session")

    assert resp.status_code == 200, resp.text
    conversation = resp.json()["conversation"]
    assert conversation["limit"] == 20
    assert conversation["has_more"] is True
    assert conversation["order"] == "oldest_first"
    contents = [item["content"] for item in conversation["items"]]
    assert contents == [f"session row {index:02d}" for index in range(1, 21)]


@pytest.mark.asyncio
async def test_strategic_session_includes_requested_older_message_once(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    other_project = await _make_project(db_session)
    base_time = _utcnow() - timedelta(hours=2)
    target = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="strategic",
        content="older targeted question",
        status="open",
        created_at=base_time,
    )
    newer_messages = [
        ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="user" if index % 2 == 0 else "strategic",
            content=f"newer session row {index:02d}",
            created_at=base_time + timedelta(minutes=index + 1),
        )
        for index in range(20)
    ]
    other_message = ProjectMessage(
        project_id=other_project.id,
        message_type=MessageType.QUESTION,
        from_type="strategic",
        content="other project question",
        status="open",
        created_at=base_time,
    )
    db_session.add_all([target, *newer_messages, other_message])
    await db_session.commit()

    resp = await client.get(
        f"/api/admin/projects/{project.id}/strategic-session"
        f"?include_message_id={target.id}"
    )

    assert resp.status_code == 200, resp.text
    conversation = resp.json()["conversation"]
    ids = [item["id"] for item in conversation["items"]]
    assert conversation["limit"] == 20
    assert conversation["has_more"] is True
    assert conversation["order"] == "oldest_first"
    assert ids.count(str(target.id)) == 1
    assert ids[0] == str(target.id)
    assert [item["content"] for item in conversation["items"][1:]] == [
        f"newer session row {index:02d}" for index in range(20)
    ]

    cross_project_resp = await client.get(
        f"/api/admin/projects/{project.id}/strategic-session"
        f"?include_message_id={other_message.id}"
    )
    assert cross_project_resp.status_code == 200, cross_project_resp.text
    cross_project_ids = [
        item["id"] for item in cross_project_resp.json()["conversation"]["items"]
    ]
    assert str(other_message.id) not in cross_project_ids


@pytest.mark.asyncio
async def test_strategic_session_conversation_filters_routine_before_limit(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    base_time = _utcnow() - timedelta(hours=2)
    chat_messages = [
        ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="user",
            content="operator asks about the workday",
            created_at=base_time,
        ),
        ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="strategic",
            content="strategic replies with the current plan",
            created_at=base_time + timedelta(minutes=1),
        ),
    ]
    routine_messages = [
        ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="system",
            content=f"routine status row {index:02d}",
            context={"event_type": "pr_merged"},
            created_at=base_time + timedelta(hours=1, minutes=index),
        )
        for index in range(25)
    ]
    db_session.add_all([*chat_messages, *routine_messages])
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-session")

    assert resp.status_code == 200, resp.text
    conversation = resp.json()["conversation"]
    assert conversation["has_more"] is False
    assert [item["content"] for item in conversation["items"]] == [
        "operator asks about the workday",
        "strategic replies with the current plan",
    ]


@pytest.mark.asyncio
async def test_strategic_session_create_persists_project_session(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)

    resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session",
        json={
            "workbench": {
                "interaction_mode": "check_in",
                "active_surface": "project",
                "selected_refs": [
                    {
                        "kind": "direction",
                        "id": "d-high",
                        "label": "Operator workbench direction",
                    }
                ],
            }
        },
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["arming"] == "steering"
    assert data["session"]["mode"] == "check_in"
    assert data["session"]["active_surface"] == "project"
    assert data["session"]["selected_refs"] == [
        {
            "kind": "direction",
            "id": "d-high",
            "label": "Operator workbench direction",
        }
    ]
    assert data["session"]["status"] == "unavailable"
    assert data["session"]["location"] == "unavailable"

    stored = (
        await db_session.execute(
            select(MessagesNoisePrefs).where(
                MessagesNoisePrefs.user_id == TEST_USER_ID,
                MessagesNoisePrefs.project_id == project.id,
            )
        )
    ).scalar_one()
    assert stored.prefs["_strategic_workbench"]["interaction_mode"] == "check_in"
    await db_session.refresh(project)
    marker = (project.vision or {})["_strategic_refresh"]
    assert marker["reason"] == "workbench_interaction_mode"
    assert marker["origin"] == "operator"


@pytest.mark.asyncio
async def test_strategic_session_message_records_operator_message_and_snapshot(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    await client.post(
        f"/api/admin/projects/{project.id}/strategic-session",
        json={
            "workbench": {
                "interaction_mode": "monitor",
                "active_surface": "pipeline",
                "selected_refs": [
                    {"kind": "task", "id": "task-9", "label": "Watch task"}
                ],
            }
        },
    )

    body = {
        "message_type": "FEEDBACK",
        "content": "Keep this session pinned to the local Strategic runner.",
        "idempotency_key": "strategic-session-message-1",
    }
    with patch(
        "app.routers.admin_projects.broadcast_to_project",
        new_callable=AsyncMock,
    ) as broadcast:
        first = await client.post(
            f"/api/admin/projects/{project.id}/strategic-session/messages",
            json=body,
        )
        second = await client.post(
            f"/api/admin/projects/{project.id}/strategic-session/messages",
            json=body,
        )

    assert first.status_code == 200, first.text
    assert first.json()["status"] == "created"
    assert first.json()["arming"] == "steering"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert second.json()["id"] == first.json()["id"]
    broadcast.assert_awaited_once_with(
        str(project.id),
        "message_created",
        {
            "message_id": first.json()["id"],
            "type": "FEEDBACK",
            "from": "user",
        },
    )

    msg = await db_session.get(ProjectMessage, uuid.UUID(first.json()["id"]))
    assert msg.from_type == "user"
    assert msg.message_type == MessageType.FEEDBACK
    assert msg.idempotency_key == "strategic-session-message-1"
    session_snapshot = (msg.context or {})["strategic_session"]
    assert session_snapshot["interaction_mode"] == "monitor"
    assert session_snapshot["active_surface"] == "pipeline"
    assert session_snapshot["selected_refs"] == [
        {"kind": "task", "id": "task-9", "label": "Watch task"}
    ]
    count = await db_session.scalar(
        select(func.count())
        .select_from(ProjectMessage)
        .where(ProjectMessage.project_id == project.id)
        .where(ProjectMessage.idempotency_key == "strategic-session-message-1")
    )
    assert count == 1


@pytest.mark.asyncio
async def test_strategic_session_message_response_enriches_selected_refs(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    long_content = "Use the canvas context when replying. " + ("y" * 500)

    body = {
        "message_type": "FEEDBACK",
        "content": long_content,
        "idempotency_key": "strategic-session-full-receipt",
        "workbench_context": {
            "interaction_mode": "continuous",
            "active_surface": "project",
            "context_package_id": f"strategic-context:{project.id}",
            "selected_refs": [
                {"kind": "canvas", "id": f"strategic-canvas:{project.id}"}
            ],
        },
    }
    resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session/messages",
        json=body,
    )
    duplicate = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session/messages",
        json=body,
    )

    assert resp.status_code == 200, resp.text
    assert duplicate.status_code == 200, duplicate.text
    message = resp.json()["message"]
    duplicate_message = duplicate.json()["message"]
    assert message["content"] == long_content
    assert duplicate.json()["status"] == "duplicate"
    assert duplicate_message["content"] == long_content
    strategic_session = message["context"]["strategic_session"]
    duplicate_session = duplicate_message["context"]["strategic_session"]
    assert strategic_session["context_package_id"] == f"strategic-context:{project.id}"
    assert duplicate_session["context_package_id"] == f"strategic-context:{project.id}"
    descriptors = strategic_session["selected_ref_descriptors"]
    duplicate_descriptors = duplicate_session["selected_ref_descriptors"]
    assert len(descriptors) == 1
    assert descriptors[0]["kind"] == "canvas"
    assert descriptors[0]["id"] == f"strategic-canvas:{project.id}"
    assert descriptors[0]["label"] == "Strategic context map"
    assert descriptors[0]["summary"]
    assert descriptors[0]["target"] == {
        "surface": "project",
        "section": "problem-model",
    }
    assert duplicate_descriptors == descriptors
    stored = await db_session.get(ProjectMessage, uuid.UUID(resp.json()["id"]))
    assert stored is not None
    assert (
        (stored.context or {})["strategic_session"]["context_package_id"]
        == f"strategic-context:{project.id}"
    )
    prefs = (
        await db_session.execute(
            select(MessagesNoisePrefs).where(
                MessagesNoisePrefs.user_id == TEST_USER_ID,
                MessagesNoisePrefs.project_id == project.id,
            )
        )
    ).scalar_one()
    stored_workbench = (prefs.prefs or {}).get("_strategic_workbench") or {}
    assert "context_package_id" not in stored_workbench


@pytest.mark.asyncio
async def test_strategic_session_message_response_survives_arming_rollback(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)

    async def _rollback_and_decline(session, *_args, **_kwargs):
        await session.rollback()
        return "not_armed"

    with patch(
        "app.routers.admin_projects.maybe_arm_operator_refresh",
        side_effect=_rollback_and_decline,
    ):
        resp = await client.post(
            f"/api/admin/projects/{project.id}/strategic-session/messages",
            json={
                "message_type": "FEEDBACK",
                "content": "This should still return a durable message receipt.",
            },
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "created"
    assert data["arming"] == "not_armed"
    assert data["message"]["id"] == data["id"]
    assert data["message"]["content"] == "This should still return a durable message receipt."


@pytest.mark.asyncio
async def test_strategic_session_note_message_is_passive(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)

    resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session/messages",
        json={
            "message_type": "NOTE",
            "content": "Keep this note with the persistent Strategic session.",
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "created"
    assert resp.json()["arming"] == "passive_note"
    msg = await db_session.get(ProjectMessage, uuid.UUID(resp.json()["id"]))
    assert msg.from_type == "user"
    assert msg.message_type == MessageType.NOTE
    assert msg.content == "Keep this note with the persistent Strategic session."


@pytest.mark.asyncio
async def test_strategic_session_message_touches_workbench_freshness(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    old_interaction_at = (_utcnow() - timedelta(hours=1)).isoformat()
    db_session.add(
        MessagesNoisePrefs(
            user_id=TEST_USER_ID,
            project_id=project.id,
            prefs={
                "_strategic_workbench": {
                    "version": 1,
                    "interaction_mode": "monitor",
                    "active_surface": "pipeline",
                    "selected_refs": [
                        {"kind": "task", "id": "task-9", "label": "Watch task"}
                    ],
                    "session_started_at": old_interaction_at,
                    "last_interaction_at": old_interaction_at,
                }
            },
        )
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session/messages",
        json={
            "message_type": "NOTE",
            "content": "Keep the persistent Strategic session alive.",
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["arming"] == "passive_note"
    stored = (
        await db_session.execute(
            select(MessagesNoisePrefs).where(
                MessagesNoisePrefs.user_id == TEST_USER_ID,
                MessagesNoisePrefs.project_id == project.id,
            )
        )
    ).scalar_one()
    workbench = stored.prefs["_strategic_workbench"]
    assert workbench["interaction_mode"] == "monitor"
    assert workbench["active_surface"] == "pipeline"
    assert workbench["selected_refs"] == [
        {"kind": "task", "id": "task-9", "label": "Watch task"}
    ]
    assert datetime.fromisoformat(workbench["last_interaction_at"]) > datetime.fromisoformat(old_interaction_at)


@pytest.mark.asyncio
async def test_strategic_state_includes_runtime_background_work(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    now = _utcnow()
    active_task = Task(
        project_id=project.id,
        title="Run GPU benchmark",
        payloads=[{"type": "compute_experiment"}],
        status=TaskStatus.IN_PROGRESS,
    )
    held_task = Task(
        project_id=project.id,
        title="Approve expensive benchmark",
        payloads=[{"type": "compute_experiment"}],
        status=TaskStatus.QUEUED,
        requires_approval=True,
        priority=1,
    )
    parked_task = Task(
        project_id=project.id,
        title="Waiting for compute result",
        payloads=[{"type": "compute_experiment"}],
        status=TaskStatus.PARKED,
        park_reason="waiting on compute job",
    )
    db_session.add_all([active_task, held_task, parked_task])
    await db_session.commit()
    await db_session.refresh(active_task)
    await db_session.refresh(parked_task)

    active_job = ComputeJob(
        project_id=project.id,
        task_id=active_task.id,
        backend="local",
        machine_type="gpu-small",
        accelerator_type="L4",
        accelerator_count=1,
        status=ComputeJobStatus.RUNNING,
        spend_cap_usd=8.0,
        accrued_cost_usd=1.25,
        started_at=now - timedelta(minutes=20),
        updated_at=now,
        last_heartbeat_at=now - timedelta(seconds=30),
    )
    failed_job = ComputeJob(
        project_id=project.id,
        task_id=active_task.id,
        backend="local",
        status=ComputeJobStatus.FAILED,
        final_cost_usd=2.5,
        completed_at=now - timedelta(minutes=10),
        updated_at=now - timedelta(minutes=10),
        terminal_reason="command failed " + ("x" * 500),
    )
    old_failed_job = ComputeJob(
        project_id=project.id,
        backend="local",
        status=ComputeJobStatus.FAILED,
        final_cost_usd=10.0,
        completed_at=now - timedelta(hours=12),
        updated_at=now - timedelta(hours=12),
    )
    db_session.add_all([active_job, failed_job, old_failed_job])
    await db_session.commit()
    await db_session.refresh(active_job)

    pending_wake = TaskWakeRegistration(
        task_id=parked_task.id,
        project_id=project.id,
        wake_source_kind="compute_job_terminal",
        wake_source_ref=str(active_job.id),
        wake_action=WakeAction.FINALIZE,
        delivery_state=WakeDeliveryState.PENDING,
        requested_at=now - timedelta(minutes=5),
        deadline_at=now + timedelta(hours=1),
    )
    expired_wake = TaskWakeRegistration(
        task_id=parked_task.id,
        project_id=project.id,
        wake_source_kind="compute_job_terminal",
        wake_source_ref=str(failed_job.id),
        wake_action=WakeAction.FINALIZE,
        delivery_state=WakeDeliveryState.EXPIRED,
        requested_at=now - timedelta(minutes=15),
        deadline_at=now - timedelta(minutes=1),
        result_state="expired",
        result_detail="wake expired after compute job finished " + ("y" * 500),
    )
    old_expired_wake = TaskWakeRegistration(
        task_id=parked_task.id,
        project_id=project.id,
        wake_source_kind="compute_job_terminal",
        wake_source_ref="old-job",
        wake_action=WakeAction.FINALIZE,
        delivery_state=WakeDeliveryState.EXPIRED,
        requested_at=now - timedelta(hours=12),
        deadline_at=now - timedelta(hours=11),
        result_state="expired",
        result_detail="old wake failure should not keep the rail hot",
    )
    db_session.add_all([pending_wake, expired_wake, old_expired_wake])
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    data = resp.json()

    assert data["tasks"]["counts"]["parked"] == 1
    work = data["runtime"]["work"]
    compute = work["compute_jobs"]
    assert compute["window_minutes"] == 360
    assert compute["counts"]["active"] == 1
    assert compute["counts"]["running"] == 1
    assert compute["counts"]["failed_recent"] == 1
    assert compute["counts"]["terminal_recent"] == 1
    assert compute["counts"]["total"] == 2
    assert compute["spend"]["active_accrued_usd"] == 1.25
    assert compute["spend"]["recent_terminal_usd"] == 2.5
    assert {item["status"] for item in compute["items"]} == {"RUNNING", "FAILED"}
    running_item = next(item for item in compute["items"] if item["status"] == "RUNNING")
    assert running_item["task_title"] == "Run GPU benchmark"
    assert running_item["accelerator_type"] == "L4"
    failed_item = next(item for item in compute["items"] if item["status"] == "FAILED")
    assert len(failed_item["terminal_reason"]) <= 180

    assert work["held_approvals"]["count"] == 1
    assert work["held_approvals"]["items"][0]["title"] == "Approve expensive benchmark"
    wakes = work["parked_wakes"]
    assert wakes["counts"]["active"] == 1
    assert wakes["counts"]["pending"] == 1
    assert wakes["counts"]["problem"] == 1
    assert wakes["counts"]["expired"] == 1
    assert {item["delivery_state"] for item in wakes["items"]} == {
        "PENDING",
        "EXPIRED",
    }
    assert all(item["wake_source_ref"] != "old-job" for item in wakes["items"])

    session_metrics = {
        entry["id"]: entry for entry in data["work_session"]["curated_metrics"]
    }
    assert session_metrics["tasks.parked"]["value"] == 1
    assert session_metrics["runtime.compute_active"]["value"] == 1
    assert session_metrics["runtime.compute_active"]["target"] == {
        "surface": "runtime",
        "section": "compute",
    }
    assert session_metrics["runtime.compute_failed"]["value"] == 1
    assert session_metrics["runtime.compute_failed"]["tone"] == "critical"
    assert session_metrics["runtime.held_approvals"]["value"] == 1
    assert session_metrics["runtime.held_approvals"]["target"] == {
        "surface": "pipeline",
    }
    assert session_metrics["runtime.parked_wakes"]["value"] == 1
    assert session_metrics["runtime.wake_problems"]["value"] == 1
    assert session_metrics["runtime.compute_spend"]["unit"] == "USD"
    assert session_metrics["runtime.compute_spend"]["value"] == pytest.approx(3.75)
    session_actions = {
        entry["id"]: entry for entry in data["work_session"]["actions"]
    }
    compute_action = session_actions["runtime.compute.problem_recent"]
    assert compute_action["kind"] == "inspect"
    assert compute_action["priority"] == "high"
    assert compute_action["source"] == "runtime"
    assert compute_action["target"] == {
        "surface": "runtime",
        "section": "compute",
    }
    expired_item = next(item for item in wakes["items"] if item["delivery_state"] == "EXPIRED")
    assert len(expired_item["result_detail"]) <= 180


@pytest.mark.asyncio
async def test_strategic_state_surfaces_local_runner_holds(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    awaiting_since = (_utcnow() - timedelta(minutes=45)).isoformat()
    held_for_runner = Task(
        project_id=project.id,
        title="Run local-only GPU benchmark",
        payloads=[{"type": "compute_experiment"}],
        status=TaskStatus.QUEUED,
        latest_status_message="require_local_runner is waiting for hardware.",
        blocked_reason="No eligible local runner is online.",
        park_reason="Waiting for self-hosted hardware.",
        context={"awaiting_local_runner_since": awaiting_since},
        priority=1,
    )
    approval_hold = Task(
        project_id=project.id,
        title="Approve local-only benchmark",
        payloads=[{"type": "compute_experiment"}],
        status=TaskStatus.QUEUED,
        requires_approval=True,
        context={"awaiting_local_runner_since": awaiting_since},
        priority=2,
    )
    archived_hold = Task(
        project_id=project.id,
        title="Archived hold",
        payloads=[{"type": "compute_experiment"}],
        status=TaskStatus.QUEUED,
        archived=True,
        context={"awaiting_local_runner_since": awaiting_since},
    )
    wrong_type_hold = Task(
        project_id=project.id,
        title="Non-compute hold marker",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        context={"awaiting_local_runner_since": awaiting_since},
    )
    running_hold_marker = Task(
        project_id=project.id,
        title="Running compute marker",
        payloads=[{"type": "compute_experiment"}],
        status=TaskStatus.IN_PROGRESS,
        context={"awaiting_local_runner_since": awaiting_since},
    )
    db_session.add_all(
        [
            held_for_runner,
            approval_hold,
            archived_hold,
            wrong_type_hold,
            running_hold_marker,
        ]
    )
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    data = resp.json()

    holds = data["runtime"]["work"]["local_runner_holds"]
    assert holds["count"] == 1
    hold_items = {item["id"]: item for item in holds["items"]}
    assert set(hold_items) == {str(held_for_runner.id)}
    held_item = hold_items[str(held_for_runner.id)]
    assert held_item["status"] == "QUEUED"
    assert held_item["title"] == "Run local-only GPU benchmark"
    assert held_item["task_type"] == "compute_experiment"
    assert held_item["awaiting_since"] == awaiting_since
    assert held_item["blocked_reason"] == "No eligible local runner is online."
    assert held_item["park_reason"] == "Waiting for self-hosted hardware."
    assert held_item["latest_status_message"] == (
        "require_local_runner is waiting for hardware."
    )
    assert held_item["created_at"]
    assert held_item["updated_at"]

    metrics = {
        entry["id"]: entry for entry in data["work_session"]["curated_metrics"]
    }
    assert metrics["runtime.local_runner_holds"]["value"] == 1
    assert metrics["runtime.local_runner_holds"]["tone"] == "warning"
    assert metrics["runtime.local_runner_holds"]["target"] == {
        "surface": "runtime",
        "section": "devices",
    }

    attention = data["attention"]
    assert attention["level"] == "needs_operator"
    assert attention["summary"] == "2 operator items need review"
    assert attention["counts"]["approvals"] == 1
    assert attention["counts"]["local_runner_holds"] == 1
    assert attention["counts"]["needs_operator"] == 2
    assert attention["counts"]["watch"] == 0
    attention_items = {item["id"]: item for item in attention["items"]}
    assert f"task:{held_for_runner.id}:local_runner_hold" in attention_items
    assert f"task:{approval_hold.id}:approval_required" in attention_items
    assert f"task:{approval_hold.id}:local_runner_hold" not in attention_items
    local_attention = attention_items[f"task:{held_for_runner.id}:local_runner_hold"]
    assert local_attention["reason"] == "local_runner_hold"
    assert local_attention["label"] == "Local runner needed"
    assert local_attention["severity"] == "needs_operator"
    assert local_attention["awaiting_since"] == awaiting_since
    assert "Waiting for local runner hardware" in local_attention["detail"]

    actions = {entry["id"]: entry for entry in data["work_session"]["actions"]}
    hold_action = actions[f"attention.task:{held_for_runner.id}:local_runner_hold"]
    assert hold_action["kind"] == "inspect"
    assert hold_action["label"] == "Check local runner"
    assert hold_action["priority"] == "critical"
    assert hold_action["target"] == {
        "surface": "runtime",
        "project_id": str(project.id),
        "task_id": str(held_for_runner.id),
        "section": "devices",
    }
    assert "runtime.local_runner_holds" not in actions


@pytest.mark.asyncio
async def test_strategic_state_does_not_return_terminal_task_as_active(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    failed_task = Task(
        project_id=project.id,
        title="Failed Strategic review",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.FAILED,
        strategic_cycle_id="sc-failed-task",
    )
    db_session.add(failed_task)
    await db_session.commit()
    await db_session.refresh(failed_task)
    stale_cycle = StrategicCycle(
        id="sc-failed-task",
        project_id=project.id,
        outcome="phase2_running",
        phase1_started_at=_utcnow() - timedelta(hours=1),
        phase2_task_id=failed_task.id,
    )
    db_session.add(stale_cycle)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    strategic = resp.json()["strategic"]
    assert strategic["active_cycle"]["id"] == "sc-failed-task"
    assert strategic["active_cycle"]["has_active_task"] is False
    assert strategic["active_cycle"]["stale"] is True
    assert strategic["active_task"] is None
    assert strategic["active_phase"] is None


@pytest.mark.asyncio
async def test_strategic_state_task_counts_reconcile_with_parked(
    client: AsyncClient, db_session
):
    """Task counts reconcile: sum of all buckets equals total, parked work is visible."""
    project = await _make_project(db_session)
    parked_task = Task(
        project_id=project.id,
        title="Parked: waiting for external signal",
        payloads=[{"type": "create"}],
        status=TaskStatus.PARKED,
        park_reason="Awaiting external webhook",
    )
    queued_task = Task(
        project_id=project.id,
        title="Queued follow-up",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
    )
    running_task = Task(
        project_id=project.id,
        title="Running task",
        payloads=[{"type": "create"}],
        status=TaskStatus.IN_PROGRESS,
    )
    blocked_task = Task(
        project_id=project.id,
        title="Blocked task",
        payloads=[{"type": "create"}],
        status=TaskStatus.BLOCKED,
        blocked_reason="Waiting for dependency",
    )
    db_session.add_all([parked_task, queued_task, running_task, blocked_task])
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    counts = resp.json()["tasks"]["counts"]

    assert counts["parked"] == 1
    assert counts["queued"] == 1
    assert counts["running"] == 1
    assert counts["blocked"] == 1
    assert counts["failed"] == 0
    assert counts["evaluating"] == 0
    assert counts["completed"] == 0

    bucket_sum = (
        counts["queued"]
        + counts["running"]
        + counts["evaluating"]
        + counts["blocked"]
        + counts["parked"]
        + counts["failed"]
        + counts["completed"]
    )
    assert bucket_sum == counts["total"]
    assert counts["total"] == 4


@pytest.mark.asyncio
async def test_strategic_state_includes_operator_attention(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    project.directions = [
        *(project.directions or []),
        {
            "id": "d-manual",
            "text": "Resolve the product direction conflict before spawning more work.",
            "priority": "critical",
            "status": "active",
            "needs_manual_intervention": True,
        },
    ]
    db_session.add(project)
    now = _utcnow()
    question = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="agent",
        content="Should the local Strategic runner stay enabled during the workday?",
        status="open",
        created_at=now,
    )
    blocker = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="system",
        content="Runner registration failed for the local device.",
        status="open",
        created_at=now - timedelta(minutes=10),
    )
    user_blocker = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="user",
        content="User-authored steering should not count as operator attention.",
        status="open",
        created_at=now,
    )
    approval_task = Task(
        project_id=project.id,
        title="Approve compute experiment",
        payloads=[{"type": "compute_experiment"}],
        status=TaskStatus.QUEUED,
        requires_approval=True,
        priority=5,
    )
    failed_task = Task(
        project_id=project.id,
        title="Failed UI polish",
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        latest_status_message="Viewport test failed on mobile.",
    )
    blocked_task = Task(
        project_id=project.id,
        title="Blocked pipeline work",
        payloads=[{"type": "create"}],
        status=TaskStatus.BLOCKED,
        blocked_reason="Waiting for a project setting.",
    )
    active_alert = Alert(
        project_id=project.id,
        severity=AlertSeverity.ERROR,
        title="Runner offline",
        message="The local Strategic runner has not checked in.",
        domain="runtime",
        event_type="runner_offline",
    )
    dismissed_alert = Alert(
        project_id=project.id,
        severity=AlertSeverity.ERROR,
        title="Dismissed alert",
        message="This alert should stay out of attention.",
        dismissed_at=now,
    )
    db_session.add(
        MessagesLastSeen(
            user_id=TEST_USER_ID,
            project_id=project.id,
            last_seen_at=now - timedelta(minutes=5),
        )
    )
    db_session.add_all(
        [
            question,
            blocker,
            user_blocker,
            approval_task,
            failed_task,
            blocked_task,
            active_alert,
            dismissed_alert,
        ]
    )
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    data = resp.json()
    attention = data["attention"]

    assert attention["level"] == "needs_operator"
    assert attention["summary"] == "5 operator items need review"
    assert attention["counts"]["questions"] == 1
    assert attention["counts"]["blockers"] == 1
    assert attention["counts"]["bug_reports"] == 0
    assert attention["counts"]["feedback"] == 0
    assert attention["counts"]["unread"] == 1
    assert attention["counts"]["approvals"] == 1
    assert attention["counts"]["manual_directions"] == 1
    assert attention["counts"]["alerts"] == 1
    assert attention["counts"]["error_alerts"] == 1
    assert attention["counts"]["failed_tasks"] == 1
    assert attention["counts"]["blocked_tasks"] == 1
    assert attention["counts"]["needs_operator"] == 5
    assert attention["counts"]["watch"] == 2
    assert len(attention["items"]) <= 8

    item_ids = {item["id"] for item in attention["items"]}
    assert f"message:{question.id}" in item_ids
    assert f"message:{blocker.id}" in item_ids
    assert f"message:{user_blocker.id}" not in item_ids
    assert f"task:{approval_task.id}:approval_required" in item_ids
    assert f"alert:{active_alert.id}" in item_ids
    assert f"alert:{dismissed_alert.id}" not in item_ids
    assert "direction:d-manual:manual_intervention" in item_ids
    assert all(len(item.get("detail") or "") <= 180 for item in attention["items"])

    actions = data["work_session"]["actions"]
    assert len(actions) <= 10
    assert actions[0]["id"] == f"attention.message:{question.id}"
    assert actions[0]["kind"] == "reply"
    assert actions[0]["label"] == "Reply to Strategic question"
    assert actions[0]["priority"] == "critical"
    assert actions[0]["target"] == {
        "surface": "messages",
        "project_id": str(project.id),
        "message_id": str(question.id),
        "action": "reply",
    }
    actions_by_id = {entry["id"]: entry for entry in actions}
    approval_action = actions_by_id[
        f"attention.task:{approval_task.id}:approval_required"
    ]
    assert approval_action["kind"] == "approve"
    assert approval_action["priority"] == "high"
    assert approval_action["target"] == {
        "surface": "task",
        "project_id": str(project.id),
        "task_id": str(approval_task.id),
        "action": "approve",
    }
    failed_action = actions_by_id[f"attention.task:{failed_task.id}:failed_task"]
    assert failed_action["kind"] == "review"
    assert failed_action["target"]["task_id"] == str(failed_task.id)
    assert all(entry["enabled"] is True for entry in actions)
    assert all(len(entry["label"]) <= 96 for entry in actions)
    assert all(len(entry["reason"]) <= 180 for entry in actions)


@pytest.mark.asyncio
async def test_operator_attention_endpoint_reuses_projection(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    task = Task(
        project_id=project.id,
        title="Approval Task",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        requires_approval=True,
    )
    db_session.add(task)
    await db_session.commit()

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["level"] == "needs_operator"
    assert data["counts"]["approvals"] == 1
    assert data["items"][0]["id"] == f"task:{task.id}:approval_required"


@pytest.mark.asyncio
async def test_operator_attention_message_counts_are_not_capped_by_preview_limit(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    for idx in range(30):
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                message_type=MessageType.FEEDBACK,
                from_type="agent",
                content=f"Recent feedback {idx}",
                status="open",
                created_at=_utcnow() + timedelta(minutes=idx),
            )
        )
    old_question = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="agent",
        content="Older open question must still count.",
        status="open",
        created_at=_utcnow() - timedelta(days=1),
    )
    approval_task = Task(
        project_id=project.id,
        title="Approval survives feedback flood",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        requires_approval=True,
    )
    db_session.add(old_question)
    db_session.add(approval_task)
    await db_session.commit()

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention = resp.json()

    assert attention["level"] == "needs_operator"
    assert attention["counts"]["questions"] == 1
    assert attention["counts"]["feedback"] == 30
    assert attention["counts"]["approvals"] == 1
    assert attention["counts"]["needs_operator"] == 2
    assert attention["counts"]["watch"] == 30
    item_ids = {item["id"] for item in attention["items"]}
    assert f"task:{approval_task.id}:approval_required" in item_ids


@pytest.mark.asyncio
async def test_operator_attention_surfaces_bug_and_feedback_as_watch_items(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    bug = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BUG_REPORT,
        from_type="agent",
        content="The pipeline chart failed to render.",
        status="open",
    )
    feedback = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.FEEDBACK,
        from_type="strategic",
        content="Strategic suggests watching runtime health.",
        status="open",
    )
    db_session.add_all([bug, feedback])
    await db_session.commit()

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention = resp.json()

    assert attention["level"] == "watch"
    assert attention["counts"]["bug_reports"] == 1
    assert attention["counts"]["feedback"] == 1
    item_ids = {item["id"] for item in attention["items"]}
    assert f"message:{bug.id}" in item_ids
    assert f"message:{feedback.id}" in item_ids


@pytest.mark.asyncio
async def test_operator_attention_ignores_inert_manual_intervention_directions(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    project.directions = [
        *(project.directions or []),
        {
            "id": "d-active-manual",
            "text": "Active direction needing manual intervention.",
            "priority": "critical",
            "status": "active",
            "needs_manual_intervention": True,
        },
        {
            "id": "d-expired-manual",
            "text": "Expired direction needing manual intervention.",
            "priority": "high",
            "status": "expired",
            "needs_manual_intervention": True,
        },
        {
            "id": "d-completed-manual",
            "text": "Completed direction needing manual intervention.",
            "priority": "high",
            "status": "completed",
            "needs_manual_intervention": True,
        },
        {
            "id": "d-proposed-manual",
            "text": "Proposed direction needing manual intervention.",
            "priority": "medium",
            "status": "proposed",
            "needs_manual_intervention": True,
        },
        {
            "id": "d-staged-manual",
            "text": "Staged direction needing manual intervention.",
            "priority": "medium",
            "status": "staged",
            "needs_manual_intervention": True,
        },
    ]
    db_session.add(project)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    attention = resp.json()["attention"]

    assert attention["counts"]["manual_directions"] == 1
    assert attention["counts"]["needs_operator"] == 1
    item_ids = {item["id"] for item in attention["items"]}
    assert "direction:d-active-manual:manual_intervention" in item_ids
    assert "direction:d-expired-manual:manual_intervention" not in item_ids
    assert "direction:d-completed-manual:manual_intervention" not in item_ids
    assert "direction:d-proposed-manual:manual_intervention" not in item_ids
    assert "direction:d-staged-manual:manual_intervention" not in item_ids


@pytest.mark.asyncio
async def test_operator_attention_approval_precedence_over_blocked(
    client: AsyncClient, db_session
):
    """Approval-required BLOCKED/PARKED tasks must appear only in the approval
    bucket — not double-counted in blocked_tasks/parked_tasks or watch."""
    project = await _make_project(db_session)
    blocked_approval_task = Task(
        project_id=project.id,
        title="Blocked but needs approval",
        payloads=[{"type": "create"}],
        status=TaskStatus.BLOCKED,
        requires_approval=True,
        blocked_reason="Waiting for approval",
    )
    parked_approval_task = Task(
        project_id=project.id,
        title="Parked but needs approval",
        payloads=[{"type": "create"}],
        status=TaskStatus.PARKED,
        requires_approval=True,
        park_reason="Approval pending",
    )
    regular_approval_task = Task(
        project_id=project.id,
        title="Regular approval task",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        requires_approval=True,
    )
    db_session.add_all(
        [blocked_approval_task, parked_approval_task, regular_approval_task]
    )
    await db_session.commit()

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention = resp.json()

    # All three approval tasks counted under approvals/needs_operator only
    assert attention["counts"]["approvals"] == 3
    assert attention["counts"]["blocked_tasks"] == 0
    assert attention["counts"]["parked_tasks"] == 0
    assert attention["counts"]["needs_operator"] == 3
    assert attention["counts"]["watch"] == 0

    item_ids = {item["id"] for item in attention["items"]}
    # All three surface as approval_required items
    assert f"task:{regular_approval_task.id}:approval_required" in item_ids
    assert f"task:{blocked_approval_task.id}:approval_required" in item_ids
    assert f"task:{parked_approval_task.id}:approval_required" in item_ids
    # None surface as blocked_task / parked_task
    assert f"task:{blocked_approval_task.id}:blocked_task" not in item_ids
    assert f"task:{parked_approval_task.id}:parked_task" not in item_ids


@pytest.mark.asyncio
async def test_operator_attention_pure_blocked_not_requiring_approval_stays_in_watch(
    client: AsyncClient, db_session
):
    """A BLOCKED task with requires_approval=False must remain in blocked_count/watch,
    confirming the approval-precedence guard only fires on requires_approval=True."""
    project = await _make_project(db_session)
    plain_blocked = Task(
        project_id=project.id,
        title="Blocked without approval flag",
        payloads=[{"type": "create"}],
        status=TaskStatus.BLOCKED,
        requires_approval=False,
        blocked_reason="Waiting for a project setting.",
    )
    db_session.add(plain_blocked)
    await db_session.commit()

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention = resp.json()

    assert attention["counts"]["approvals"] == 0
    assert attention["counts"]["blocked_tasks"] == 1
    assert attention["counts"]["needs_operator"] == 0
    assert attention["counts"]["watch"] == 1

    item_ids = {item["id"] for item in attention["items"]}
    assert f"task:{plain_blocked.id}:blocked_task" in item_ids
    assert f"task:{plain_blocked.id}:approval_required" not in item_ids


@pytest.mark.asyncio
async def test_operator_attention_no_last_seen_all_messages_unread(
    client: AsyncClient, db_session
):
    """When no MessagesLastSeen row exists, every high-signal non-user message is unread."""
    project = await _make_project(db_session)
    for i in range(3):
        db_session.add(ProjectMessage(
            project_id=project.id,
            message_type=MessageType.QUESTION,
            from_type="agent",
            content=f"Open question {i}",
            status="open",
        ))
    db_session.add(ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="system",
        content="System blocker",
        status="open",
    ))
    await db_session.commit()

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention = resp.json()

    assert attention["last_seen_at"] is None
    # 3 questions + 1 blocker = 4 high-signal messages, all unread
    assert attention["counts"]["unread"] == 4
    # Every rendered message item should have unread=True
    message_items = [item for item in attention["items"] if item["kind"] == "message"]
    assert all(item["unread"] is True for item in message_items)


@pytest.mark.asyncio
async def test_operator_attention_last_seen_boundary_exact_created_at_is_not_unread(
    client: AsyncClient, db_session
):
    """Messages created exactly AT last_seen_at are NOT unread; only strictly after."""
    project = await _make_project(db_session)
    boundary_time = _utcnow()
    exact_msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="agent",
        content="Created exactly at boundary — should not be unread.",
        status="open",
        created_at=boundary_time,
    )
    after_msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="agent",
        content="Created strictly after boundary — should be unread.",
        status="open",
        created_at=boundary_time + timedelta(seconds=1),
    )
    db_session.add_all([exact_msg, after_msg])
    db_session.add(MessagesLastSeen(
        user_id=TEST_USER_ID,
        project_id=project.id,
        last_seen_at=boundary_time,
    ))
    await db_session.commit()

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention = resp.json()

    # Only after_msg is strictly after last_seen_at
    assert attention["counts"]["unread"] == 1
    items_by_msg_id = {
        item["ref"]["id"]: item
        for item in attention["items"]
        if item["kind"] == "message"
    }
    assert items_by_msg_id[str(exact_msg.id)]["unread"] is False
    assert items_by_msg_id[str(after_msg.id)]["unread"] is True


@pytest.mark.asyncio
async def test_operator_attention_first_visit_all_messages_unread(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    first_visit_message = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="agent",
        content="Does the operator need to review this on first visit?",
        status="open",
    )
    context_direction = {
        "id": "d-context",
        "text": "Direction inherited manual intervention from its context.",
        "priority": "critical",
        "status": "active",
        "context": {"needs_manual_intervention": True},
    }
    project.directions = [*(project.directions or []), context_direction]
    db_session.add(project)
    db_session.add(first_visit_message)
    await db_session.commit()

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention = resp.json()
    item_ids = {item["id"] for item in attention["items"]}

    assert attention["last_seen_at"] is None
    assert attention["counts"]["unread"] == 1
    assert f"message:{first_visit_message.id}" in item_ids
    assert "direction:d-context:manual_intervention" in item_ids
    assert next(
        item
        for item in attention["items"]
        if item["id"] == f"message:{first_visit_message.id}"
    )["unread"] is True


@pytest.mark.asyncio
async def test_admin_messages_accepts_linked_status_filter(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    linked = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="system",
        content="Linked issue",
        status="linked",
    )
    open_msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="user",
        content="Open note",
        status="open",
    )
    db_session.add(linked)
    db_session.add(open_msg)
    await db_session.commit()

    resp = await client.get(
        f"/api/admin/messages?project_id={project.id}&status=linked"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert [m["content"] for m in data["messages"]] == ["Linked issue"]


@pytest.mark.asyncio
async def test_direction_attention_detail_uses_intervention_reason(
    client: AsyncClient, db_session
):
    """Direction items with needs_manual_intervention_reason use the reason as
    the attention detail instead of the generic direction text."""
    project = await _make_project(db_session)
    specific_reason = "Remediation failed after 3 attempts — fingerprint: err-abc123"
    project.directions = [
        {
            "id": "d-intervention",
            "text": "Generic direction text that should NOT appear in detail.",
            "priority": "critical",
            "status": "active",
            "needs_manual_intervention": True,
            "needs_manual_intervention_reason": specific_reason,
        }
    ]
    db_session.add(project)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    attention = resp.json()["attention"]

    direction_items = [
        item for item in attention["items"] if item["kind"] == "direction"
    ]
    assert len(direction_items) == 1
    assert direction_items[0]["detail"] == specific_reason


@pytest.mark.asyncio
async def test_strategic_state_sha_freshness_unknown_when_no_open_version(
    client: AsyncClient, db_session
):
    """No open ReleaseVersion → sha_freshness.verdict is unknown, endpoint still 200."""
    project = await _make_project(db_session)

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    freshness = resp.json()["sha_freshness"]
    assert freshness is not None
    assert freshness["verdict"] == "unknown"
    _assert_generated_at_iso_utc(freshness)
    # No SHAs present when there is no open version
    assert "active_candidate_sha" not in freshness
    assert "integration_head_sha" not in freshness


@pytest.mark.asyncio
async def test_strategic_state_exposes_sha_freshness_verdict_when_candidate_behind(
    client: AsyncClient, db_session, mocker
):
    """Open version with candidate SHA behind integration HEAD → verdict stale.

    This is the acceptance-criteria test: non-trivial mock + real merge-test
    verdict path (candidate != head_sha → current=False → verdict=stale).
    """
    project = await _make_project(db_session)
    candidate_sha = "abc1234" * 5
    head_sha = "def5678" * 5
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        opened_at=_utcnow(),
        last_qualification={"candidate_sha": candidate_sha, "status": "not_promoted"},
    )
    db_session.add(version)
    await db_session.commit()

    mocker.patch(
        "app.strategic_spawning._resolve_candidate_currency",
        new=AsyncMock(
            return_value={
                "current": False,
                "candidate_sha": candidate_sha,
                "head_sha": head_sha,
            }
        ),
    )
    mocker.patch(
        "app.strategic_spawning.get_project_integration_branch",
        return_value="main",
    )

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    freshness = resp.json()["sha_freshness"]
    assert freshness["verdict"] == "stale"
    assert freshness["active_candidate_sha"] == candidate_sha
    assert freshness["integration_head_sha"] == head_sha
    _assert_generated_at_iso_utc(freshness)


@pytest.mark.asyncio
async def test_strategic_state_sha_freshness_fresh_when_candidate_matches_head(
    client: AsyncClient, db_session, mocker
):
    """Open version where candidate == integration HEAD → verdict fresh."""
    project = await _make_project(db_session)
    sha = "aabbccdd" * 5
    version = ReleaseVersion(
        project_id=project.id,
        version_number=2,
        status="open",
        opened_at=_utcnow(),
        last_qualification={"candidate_sha": sha, "status": "not_promoted"},
    )
    db_session.add(version)
    await db_session.commit()

    mocker.patch(
        "app.strategic_spawning._resolve_candidate_currency",
        new=AsyncMock(
            return_value={
                "current": True,
                "candidate_sha": sha,
                "head_sha": sha,
            }
        ),
    )
    mocker.patch(
        "app.strategic_spawning.get_project_integration_branch",
        return_value="main",
    )

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    freshness = resp.json()["sha_freshness"]
    assert freshness["verdict"] == "fresh"
    assert freshness["active_candidate_sha"] == sha
    assert freshness["integration_head_sha"] == sha
    _assert_generated_at_iso_utc(freshness)


@pytest.mark.asyncio
async def test_strategic_state_sha_freshness_unknown_when_resolve_fails(
    client: AsyncClient, db_session, mocker
):
    """When _resolve_candidate_currency raises, sha_freshness.verdict is unknown.
    Endpoint must still return 200 (GitHub failure must not break the read model)."""
    project = await _make_project(db_session)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=3,
        status="open",
        opened_at=_utcnow(),
        last_qualification={"candidate_sha": "abc123", "status": "not_promoted"},
    )
    db_session.add(version)
    await db_session.commit()

    mocker.patch(
        "app.strategic_spawning._resolve_candidate_currency",
        new=AsyncMock(side_effect=RuntimeError("GitHub API timeout")),
    )
    mocker.patch(
        "app.strategic_spawning.get_project_integration_branch",
        return_value="main",
    )

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    freshness = resp.json()["sha_freshness"]
    assert freshness["verdict"] == "unknown"


@pytest.mark.asyncio
async def test_strategic_state_sha_freshness_includes_checked_at(
    client: AsyncClient, db_session, mocker
):
    """Freshness block for a fresh verdict carries a checked_at ISO timestamp."""
    project = await _make_project(db_session)
    sha = "ccddee11" * 5
    version = ReleaseVersion(
        project_id=project.id,
        version_number=4,
        status="open",
        opened_at=_utcnow(),
        last_qualification={"candidate_sha": sha, "status": "not_promoted"},
    )
    db_session.add(version)
    await db_session.commit()

    mocker.patch(
        "app.strategic_spawning._resolve_candidate_currency",
        new=AsyncMock(
            return_value={
                "current": True,
                "candidate_sha": sha,
                "head_sha": sha,
            }
        ),
    )
    mocker.patch(
        "app.strategic_spawning.get_project_integration_branch",
        return_value="main",
    )

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    freshness = resp.json()["sha_freshness"]
    assert freshness["verdict"] == "fresh"
    assert "checked_at" in freshness
    assert freshness["checked_at"] is not None
    # ISO-8601 string sanity check
    assert "T" in freshness["checked_at"]


@pytest.mark.asyncio
async def test_strategic_state_sha_freshness_includes_integration_branch(
    client: AsyncClient, db_session, mocker
):
    """Stale freshness block carries the integration_branch name used for comparison."""
    project = await _make_project(db_session)
    candidate_sha = "aabb1122" * 5
    head_sha = "ccdd3344" * 5
    version = ReleaseVersion(
        project_id=project.id,
        version_number=5,
        status="open",
        opened_at=_utcnow(),
        last_qualification={"candidate_sha": candidate_sha, "status": "not_promoted"},
    )
    db_session.add(version)
    await db_session.commit()

    mocker.patch(
        "app.strategic_spawning._resolve_candidate_currency",
        new=AsyncMock(
            return_value={
                "current": False,
                "candidate_sha": candidate_sha,
                "head_sha": head_sha,
            }
        ),
    )
    mocker.patch(
        "app.strategic_spawning.get_project_integration_branch",
        return_value="automain",
    )

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    freshness = resp.json()["sha_freshness"]
    assert freshness["verdict"] == "stale"
    assert freshness["integration_branch"] == "automain"


@pytest.mark.asyncio
async def test_strategic_state_sha_freshness_unknown_includes_checked_at(
    client: AsyncClient, db_session
):
    """Unknown freshness verdict (no open version) still carries a checked_at timestamp."""
    project = await _make_project(db_session)

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    freshness = resp.json()["sha_freshness"]
    assert freshness["verdict"] == "unknown"
    assert "checked_at" in freshness
    assert freshness["checked_at"] is not None
    assert "T" in freshness["checked_at"]
