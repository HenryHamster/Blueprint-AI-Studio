import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from app.models import (
    LocalRunner,
    MessageType,
    Project,
    ProjectMember,
    ProjectMessage,
    StrategicCycle,
    Task,
    TaskStatus,
    _utcnow,
)
from app.task_capabilities import STRATEGIC_TASK_TYPES

TEST_USER_ID = "test-user-123"
FIXTURE_DIR = Path(__file__).parent / "__fixtures__"


async def _make_project(session) -> Project:
    project = Project(
        name="Operator Workbench Contract",
        user_id=TEST_USER_ID,
        directions=[
            {
                "id": "direction-main",
                "text": "Keep operator chat and monitoring coherent.",
                "priority": "high",
                "status": "active",
                "recommended_mode": "continuous",
            }
        ],
        problem_model={
            "revision": 1,
            "intent": {
                "purpose": "Operate Blueprint through a persistent Strategic workbench.",
                "current_focus": "Local strategic runner contract",
            },
            "gaps": [
                {
                    "id": "gap-local-strategic",
                    "name": "Local strategic status must be visible",
                    "priority": "high",
                    "status": "active",
                }
            ],
            "capabilities": [{"name": "pipeline monitoring", "status": "working"}],
            "pending_user_confirmation": [],
        },
        running_update="Strategic is validating the local runner contract.",
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner"))
    await session.commit()
    return project


def _assert_has_keys(payload: dict[str, Any], keys: set[str]) -> None:
    missing = keys.difference(payload)
    assert not missing, f"missing keys: {sorted(missing)}"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_operator_workbench_current_state_contract_surfaces(
    client: AsyncClient,
    db_session,
):
    project = await _make_project(db_session)
    now = _utcnow()
    runner = LocalRunner(
        project_id=project.id,
        token_hash="c" * 64,
        hostname="operator-desk",
        version="runner-1.0",
        status="active",
        last_seen_at=now - timedelta(seconds=15),
        capabilities={
            "task_types": sorted(STRATEGIC_TASK_TYPES),
            "has_gpu": False,
            "max_concurrent": 1,
            "runner_preset": "strategic_local",
            "workload": "strategic",
            "agent_available": True,
            "active_task_ids": [],
            "active_compute_job_ids": [],
        },
    )
    db_session.add(runner)
    await db_session.flush()

    active_task = Task(
        project_id=project.id,
        title="Strategic review on local runner",
        payloads=[{"type": "strategic_review", "direction_id": "direction-main"}],
        status=TaskStatus.IN_PROGRESS,
        strategic_cycle_id="contract-active",
        latest_status_message="Reviewing operator workbench contract.",
        vm_instance_id=f"runner:{runner.id}",
        context={
            "runner_target_id": str(runner.id),
            "llm_provider": "openai",
            "llm_model": "gpt-5.4",
            "llm_account_id": "acct-local",
        },
        started_at=now - timedelta(minutes=5),
    )
    archived_task = Task(
        project_id=project.id,
        title="Archived cleanup task",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        archived=True,
        latest_status_message="Archived for the operator archive.",
        created_at=now - timedelta(days=3),
        completed_at=now - timedelta(days=2),
    )
    archive_action_task = Task(
        project_id=project.id,
        title="Terminal task for archive action",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        latest_status_message="Ready to archive.",
        created_at=now - timedelta(days=1),
        completed_at=now - timedelta(hours=20),
    )
    message = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.FEEDBACK,
        from_type="user",
        content="Keep the main workbench simple, but let me inspect details.",
        status="open",
        created_at=now - timedelta(minutes=4),
        context={
            "strategic_session": {
                "version": 1,
                "interaction_mode": "continuous",
                "active_surface": "strategic",
                "selected_refs": [
                    {
                        "kind": "task",
                        "id": str(active_task.id),
                        "label": "Strategic review on local runner",
                    }
                ],
            }
        },
    )
    db_session.add(active_task)
    db_session.add(archived_task)
    db_session.add(archive_action_task)
    db_session.add(message)
    await db_session.flush()
    runner.capabilities = {
        **(runner.capabilities or {}),
        "active_task_ids": [str(active_task.id)],
    }
    db_session.add(runner)
    db_session.add(
        StrategicCycle(
            id="contract-active",
            project_id=project.id,
            outcome="phase2_running",
            phase1_started_at=now - timedelta(minutes=12),
            phase1_completed_at=now - timedelta(minutes=8),
            phase2_started_at=now - timedelta(minutes=8),
            phase2_task_id=active_task.id,
        )
    )
    await db_session.commit()

    state_resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert state_resp.status_code == 200, state_resp.text
    state = state_resp.json()
    _assert_has_keys(
        state,
        {
            "project_id",
            "strategic",
            "runtime",
            "attention",
            "context_package",
            "workbench",
            "work_session",
            "tasks",
            "messages",
        },
    )
    work_session = state["work_session"]
    assert work_session["version"] == 1
    assert work_session["status"] == "working"
    assert work_session["location"] == "locally"
    assert work_session["events"]["version"] == 1
    assert work_session["events"]["items"]
    assert all(item["schema_version"] == 1 for item in work_session["events"]["items"])
    assert work_session["jobs"]["version"] == 1
    assert isinstance(work_session["jobs"]["items"], list)
    metrics = {entry["id"]: entry for entry in work_session["curated_metrics"]}
    for metric_id in {
        "attention.needs_operator",
        "messages.open",
        "tasks.running",
        "runtime.local_strategic_ready",
        "context.items",
    }:
        assert metric_id in metrics
        assert isinstance(metrics[metric_id]["provenance"]["surface"], str)
        assert isinstance(metrics[metric_id]["provenance"]["path"], str)

    local_runner = state["runtime"]["local_runner"]
    assert local_runner["mode"] == "local"
    assert local_runner["status"] == "working"
    assert local_runner["active_runner_id"] == str(runner.id)
    assert local_runner["active_task_pinned"] is True
    assert local_runner["active_task_runtime"]["runner_target_id"] == str(runner.id)
    assert local_runner["runners"][0]["health"]["device_status"] == "connected"
    assert local_runner["runners"][0]["health"]["active_task_count"] == 1
    runner_candidate_capabilities = local_runner["runners"][0]["capabilities"]
    assert runner_candidate_capabilities["runner_preset"] == "strategic_local"
    assert "active_task_ids" not in runner_candidate_capabilities
    assert "active_compute_job_ids" not in runner_candidate_capabilities
    assert state["context_package"]["canvas"]["kind"] == "strategic_context"
    _assert_has_keys(
        state["attention"]["counts"],
        {"needs_operator", "watch", "unread", "questions", "blockers"},
    )

    session_resp = await client.get(
        f"/api/admin/projects/{project.id}/strategic-session"
        f"?include_message_id={message.id}"
    )
    assert session_resp.status_code == 200, session_resp.text
    session_body = session_resp.json()
    assert session_body["project_id"] == str(project.id)
    assert session_body["session"]["runtime"]["active_runner_id"] == str(runner.id)
    assert session_body["session"]["curated_metrics"] == work_session["curated_metrics"]
    assert any(
        item["id"] == str(message.id)
        for item in session_body["conversation"]["items"]
    )

    archive_resp = await client.get(
        f"/api/admin/tasks?project_id={project.id}&archived=true&limit=5&offset=0"
    )
    assert archive_resp.status_code == 200, archive_resp.text
    archive_body = archive_resp.json()
    assert archive_body["total"] == 1
    assert archive_body["tasks"][0]["id"] == str(archived_task.id)
    assert archive_body["tasks"][0]["archived"] is True

    archive_action_resp = await client.post(
        f"/api/admin/tasks/{archive_action_task.id}/archive"
    )
    assert archive_action_resp.status_code == 200, archive_action_resp.text
    archived_action = archive_action_resp.json()
    assert archived_action["id"] == str(archive_action_task.id)
    assert archived_action["status"] == "COMPLETED"
    assert archived_action["archived"] is True

    unarchive_action_resp = await client.post(
        f"/api/admin/tasks/{archive_action_task.id}/unarchive"
    )
    assert unarchive_action_resp.status_code == 200, unarchive_action_resp.text
    unarchived_action = unarchive_action_resp.json()
    assert unarchived_action["id"] == str(archive_action_task.id)
    assert unarchived_action["status"] == "COMPLETED"
    assert unarchived_action["archived"] is False

    nonterminal_archive_resp = await client.post(
        f"/api/admin/tasks/{active_task.id}/archive"
    )
    assert nonterminal_archive_resp.status_code == 400

    runners_resp = await client.get(f"/api/admin/projects/{project.id}/runners")
    assert runners_resp.status_code == 200, runners_resp.text
    runner_rows = runners_resp.json()
    assert runner_rows[0]["id"] == str(runner.id)
    _assert_has_keys(
        runner_rows[0]["health"],
        {
            "device_status",
            "is_live",
            "seconds_since_last_seen",
            "active_task_count",
            "active_compute_job_count",
            "active_work_count",
            "available_slots",
            "liveness_window_seconds",
        },
    )

    attention_resp = await client.get(
        f"/api/admin/operator-attention?project_id={project.id}"
    )
    assert attention_resp.status_code == 200, attention_resp.text
    attention = attention_resp.json()
    _assert_has_keys(attention, {"level", "summary", "counts", "items"})
    _assert_has_keys(
        attention["counts"],
        {"needs_operator", "watch", "unread", "questions", "blockers"},
    )

    account_health_resp = await client.get("/api/admin/max-plan-accounts/health")
    assert account_health_resp.status_code == 200, account_health_resp.text
    account_health = account_health_resp.json()
    _assert_has_keys(
        account_health,
        {
            "accounts",
            "generated_at",
            "served_stale",
            "precomputed",
            "precompute_age_seconds",
        },
    )
    assert account_health["served_stale"] is False

    fleet_resp = await client.get("/api/admin/fleet-utilization")
    assert fleet_resp.status_code == 200, fleet_resp.text
    fleet = fleet_resp.json()
    _assert_has_keys(
        fleet,
        {
            "active_now",
            "nominal_capacity",
            "usable_capacity",
            "saturation",
            "generated_at",
            "served_stale",
            "precompute_age_seconds",
        },
    )
    assert isinstance(fleet["served_stale"], bool)


def test_strategic_context_package_v1_fixture_covers_renderer_contract():
    package = _fixture("operator_strategic_context_package_v1.json")[
        "context_package"
    ]
    descriptors = package["descriptors"]
    kinds = {descriptor["kind"] for descriptor in descriptors}

    assert {
        "context_map",
        "metric_cards",
        "table",
        "markdown",
        "text",
        "image",
        "chart",
        "plot",
        "task_detail",
        "direction_detail",
        "message_detail",
        "resource_detail",
        "unsupported",
    } <= kinds
    assert all(descriptor["schema_version"] == 1 for descriptor in descriptors)
    assert package["canvas"]["nodes"]
    assert package["canvas"]["edges"]
    assert package["resources"][0]["kind"] == "canvas"

    serialized = json.dumps(package)
    assert "storage_uri" not in serialized
    assert "gs://blueprint" not in serialized
    assert "artifact logs" not in serialized.lower()
    assert "knowledge body" not in serialized.lower()


def test_task_sideboard_context_v1_fixture_covers_pipeline_and_task_contract():
    task = _fixture("operator_task_sideboard_context_v1.json")["task"]
    sideboard = task["sideboard_context"]

    assert sideboard["task"]["status"] == "BLOCKED"
    assert sideboard["task"]["task_type"] == "create"
    assert sideboard["task"]["blocker"] == {
        "kind": "dependency",
        "reason": "Waiting for upstream verification.",
        "blocked_at": "2026-07-01T11:30:00Z",
        "recheck_after": "2026-07-01T12:30:00Z",
        "parked_at": None,
    }
    assert sideboard["task"]["runner"]["vm_instance_id"] == "agent-vm-sideboard"
    assert sideboard["task"]["runner"]["runner_target_id"] == "runner-local-1"
    assert sideboard["task"]["cost"] == {
        "estimated_vm_usd": 1.25,
        "llm_usd": 0.42,
        "llm_any_unpriced": False,
    }
    assert sideboard["task"]["next_action"]["label"] == "Resolve blocker"
    assert sideboard["pipeline"]["parent_task_id"] == "task-parent-wave"
    assert sideboard["artifacts"]["primary_pr_url"].endswith("/pull/42")
    assert sideboard["strategic"]["direction"]["id"] == "direction-passive-work"
    assert sideboard["messages"]["total_recent"] == 2
    assert {link["id"] for link in sideboard["links"]} >= {
        "pipeline",
        "parent_task",
        "direction",
        "pull_request",
    }

    serialized = json.dumps(sideboard)
    assert "problem_model" not in serialized
    assert "design_doc" not in serialized
    assert "huge" not in serialized
