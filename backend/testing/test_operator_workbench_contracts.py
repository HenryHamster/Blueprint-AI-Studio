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
from app.workbench_contracts import (
    WEBSOCKET_EMITTER_INVENTORY,
    build_operator_workbench_contract_fixture,
    normalize_target,
    validate_executable_command_descriptor,
)
from app.workbench_state import STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY

TEST_USER_ID = "test-user-123"
FIXTURE_DIR = Path(__file__).parent / "__fixtures__"
BACKEND_ROOT = Path(__file__).resolve().parents[1]
OPERATOR_WORKBENCH_FIXTURE_PATH = FIXTURE_DIR / "operator_workbench_contracts.json"


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


def _operator_workbench_fixture() -> dict:
    return json.loads(OPERATOR_WORKBENCH_FIXTURE_PATH.read_text(encoding="utf-8"))


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
            STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
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
        "tasks.starting",
        "tasks.evaluating",
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
    assert local_runner["active_task_requires_local"] is True
    assert "continuation" in local_runner
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
        {
            "needs_operator",
            "watch",
            "visible_needs_operator",
            "visible_watch",
            "unread",
            "questions",
            "blockers",
            "feedback",
            "local_runner_holds",
            "compute_local_runner_holds",
            "strategic_local_runner_holds",
            "alerts",
        },
    )
    _assert_has_keys(
        state["tasks"]["counts"],
        {
            "queued",
            "running",
            "evaluating",
            "blocked",
            "parked",
            "failed",
            "cancelled",
            "completed",
            "total",
        },
    )
    _assert_has_keys(
        state["runtime"]["work"]["local_runner_holds"],
        {"count", "compute_count", "strategic_count", "items"},
    )
    assert state["sha_freshness"] is not None

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
    for link in sideboard["links"]:
        assert normalize_target(link["target"]) == link["target"]
    pull_request = next(link for link in sideboard["links"] if link["id"] == "pull_request")
    assert pull_request["target"]["surface"] == "external"
    assert pull_request["target"]["url"].startswith("https://")

    serialized = json.dumps(sideboard)
    assert "problem_model" not in serialized
    assert "design_doc" not in serialized
    assert "huge" not in serialized


def test_operator_workbench_contract_fixture_is_current():
    assert _operator_workbench_fixture() == build_operator_workbench_contract_fixture()


def test_continuation_marker_honors_renewal_reason():
    """The continuation marker's FRESHNESS anchor (last_seen_at) advances only on
    a genuine renewal (truthy renewal_reason); a None reason is a liveness-only
    touch that records updated_at but preserves the prior freshness anchor — the
    dead-man's switch depends on freshness tracking real progress/interaction,
    not mere liveness (finding: dead renewal_reason)."""
    from app.workbench_state import (
        STRATEGIC_LOCAL_CONTINUATION_KEY,
        apply_strategic_local_continuation_marker,
    )

    old = "2020-01-01T00:00:00"

    # Truthy reason ('strategic_progress') bumps freshness to ~now.
    vision = {
        STRATEGIC_LOCAL_CONTINUATION_KEY: {
            "version": 1,
            "active": True,
            "operator_user_id": "op-1",
            "owner_session_started_at": "s-1",
            "updated_at": old,
            "last_seen_at": old,
        }
    }
    apply_strategic_local_continuation_marker(
        vision,
        {
            "interaction_mode": "continuous",
            "session_started_at": "s-1",
            "last_interaction_at": old,
        },
        operator_user_id="op-1",
        renewal_reason="strategic_progress",
    )
    bumped = vision[STRATEGIC_LOCAL_CONTINUATION_KEY]
    assert bumped["last_seen_at"] != old  # freshness advanced
    assert bumped["last_renewal_reason"] == "strategic_progress"

    # None reason = liveness-only: updated_at advances but the freshness anchor is
    # preserved, even though the workbench carries a fresh last_interaction_at
    # (the exact case that used to spuriously renew and pin authority forever).
    vision2 = {
        STRATEGIC_LOCAL_CONTINUATION_KEY: {
            "version": 1,
            "active": True,
            "operator_user_id": "op-1",
            "owner_session_started_at": "s-1",
            "updated_at": old,
            "last_seen_at": old,
        }
    }
    apply_strategic_local_continuation_marker(
        vision2,
        {
            "interaction_mode": "continuous",
            "session_started_at": "s-1",
            "last_interaction_at": "2099-01-01T00:00:00",
        },
        operator_user_id="op-1",
        renewal_reason=None,
    )
    touched = vision2[STRATEGIC_LOCAL_CONTINUATION_KEY]
    assert touched["last_seen_at"] == old  # freshness NOT advanced
    assert touched["updated_at"] != old  # liveness WAS recorded


def test_operator_workbench_fixture_attention_has_more_matches_counts():
    fixture_attention = _operator_workbench_fixture()["strategic_state_response"][
        "attention"
    ]
    total_attention = (
        fixture_attention["counts"]["needs_operator"]
        + fixture_attention["counts"]["watch"]
    )

    assert fixture_attention["has_more"] is (
        total_attention > len(fixture_attention["items"])
    )


def test_operator_workbench_fixture_validates_core_contracts():
    fixture = _operator_workbench_fixture()

    assert fixture["context_targets"]["runtime_runner_alias_normalized"] == {
        "surface": "runtime",
        "section": "devices",
        "runner_id": "runner-1",
    }
    assert normalize_target({"surface": "runtime", "section": "runners"}) == {
        "surface": "runtime",
        "section": "devices",
    }
    assert normalize_target({"surface": "runtime", "section": "unknown"}) is None
    assert normalize_target({"surface": "external", "url": "https://example.com/pr/1"}) == {
        "surface": "external",
        "url": "https://example.com/pr/1",
    }
    assert normalize_target({"surface": "external"}) is None
    assert normalize_target({"surface": "external", "url": "javascript:alert(1)"}) is None

    command = fixture["command_descriptor"]
    assert command["executable"] is True
    assert validate_executable_command_descriptor(command, project_id="proj-1") is None

    event_payload = fixture["workbench_event_payload"]
    assert event_payload["schema_version"] == 1
    assert event_payload["refetch"]["surfaces"] == ["runtime", "strategic_session"]

    assert fixture["message_arming_outcomes"] == [
        "steering",
        "passive_note",
        "routine_event",
        "not_armed",
        "duplicate",
    ]
    assert fixture["task_detail_sideboard_context"]["links"][0]["target"] == {
        "surface": "task",
        "task_id": "task-1",
    }


def test_websocket_emitter_inventory_covers_current_emitters():
    discovered_emitters = set()
    discovered_v1_producers = set()
    for path in (BACKEND_ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        rel_path = f"backend/{path.relative_to(BACKEND_ROOT).as_posix()}"
        if "broadcast_to_project" in text or "send_json(" in text:
            discovered_emitters.add(rel_path)
        if rel_path != "backend/app/workbench_events.py" and (
            "emit_workbench_refetch(" in text
            or "workbench_event_envelope(" in text
        ):
            discovered_v1_producers.add(rel_path)

    inventoried = {
        path
        for category in ("workbench_v1", "transport", "legacy_compatibility")
        for path in WEBSOCKET_EMITTER_INVENTORY[category]
    }
    inventoried_v1_producers = {
        path
        for paths in (WEBSOCKET_EMITTER_INVENTORY["workbench_v1_producers"],)
        for path in paths
    }

    assert discovered_emitters == inventoried
    assert discovered_v1_producers == inventoried_v1_producers
    assert WEBSOCKET_EMITTER_INVENTORY["workbench_v1"] == (
        "backend/app/workbench_events.py",
    )
