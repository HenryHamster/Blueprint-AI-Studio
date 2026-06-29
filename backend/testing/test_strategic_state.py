import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient

from app.models import (
    Alert,
    AlertSeverity,
    LocalRunner,
    MessageType,
    MessagesLastSeen,
    Project,
    ProjectMember,
    ProjectMessage,
    StrategicCycle,
    Task,
    TaskStatus,
    _utcnow,
)

TEST_USER_ID = "test-user-123"


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
        estimated_cost_usd=1.25,
    )
    queued_task = Task(
        project_id=project.id,
        title="Queued follow-up",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        estimated_cost_usd=0.75,
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
            "huge": "x" * 10_000,
        },
    )
    db_session.add(active_cycle)
    db_session.add(latest_cycle)
    db_session.add(open_message)
    db_session.add(linked_message)
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
    assert local_runner["fallback_reason"] == "no_runner"
    assert local_runner["strategic_runner_count"] == 0
    assert local_runner["active_task_pinned"] is False
    assert local_runner["active_task_runtime"]["runner_target_id"] is None
    linked_preview = next(
        m for m in data["messages"]["items"] if m["status"] == "linked"
    )
    assert len(linked_preview["content"]) <= 280
    assert linked_preview["context"] == {"event_type": "blocker_linked"}
    assert data["design"]["revision"] == 3
    package = data["context_package"]
    assert package["id"] == f"strategic-context:{project.id}"
    assert package["title"] == "Persistent Strategic interaction"
    assert len(package["title"]) <= 100
    assert package["status"] == "building"
    assert package["updated_at"]
    assert package["token_count"] is None
    assert package["canvas"]["kind"] == "strategic_context"
    assert package["canvas"]["node_count"] is None
    assert {source["kind"]: source["count"] for source in package["sources"]} == {
        "running_update": 1,
        "problem_model": 4,
        "directions": 2,
        "messages": 2,
        "tasks": 2,
        "design": 1,
    }
    assert package["item_count"] == 12
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
    local_runner = resp.json()["runtime"]["local_runner"]

    assert local_runner["mode"] == "local"
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
    assert local_runner["fallback_reason"] == "runner_offline"
    assert local_runner["strategic_runner_count"] == 1
    assert local_runner["live_runner_count"] == 0
    assert local_runner["eligible_live_count"] == 0
    assert local_runner["stale_runner_count"] == 1


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
    local_runner = resp.json()["runtime"]["local_runner"]

    assert local_runner["mode"] == "pending_local"
    assert local_runner["fallback_reason"] == "capability_mismatch"
    assert local_runner["strategic_runner_count"] == 1
    assert local_runner["live_runner_count"] == 1
    assert local_runner["eligible_live_count"] == 0


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
    local_runner = resp.json()["runtime"]["local_runner"]

    assert local_runner["mode"] == "pending_local"
    assert local_runner["fallback_reason"] == "runner_offline"
    assert local_runner["active_runner_id"] == str(runner.id)
    assert local_runner["active_task_pinned"] is True
    assert local_runner["live_runner_count"] == 0
    assert local_runner["eligible_live_count"] == 0


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
    local_runner = resp.json()["runtime"]["local_runner"]

    assert local_runner["mode"] == "pending_local"
    assert local_runner["fallback_reason"] == "runner_claim_pending"
    assert local_runner["active_runner_id"] == str(runner.id)
    assert local_runner["active_task_pinned"] is True
    assert local_runner["live_runner_count"] == 1
    assert local_runner["eligible_live_count"] == 1


@pytest.mark.asyncio
async def test_strategic_state_marks_active_cycle_without_live_task_as_stale(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    stale_cycle = StrategicCycle(
        id="sc-stale",
        project_id=project.id,
        outcome="phase2_running",
        phase1_started_at=_utcnow() - timedelta(hours=2),
        phase2_task_id=uuid.uuid4(),
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
    attention = resp.json()["attention"]

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
        item for item in attention["items"] if item["id"] == f"message:{first_visit_message.id}"
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
