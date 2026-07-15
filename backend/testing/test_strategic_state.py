import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import func
from sqlmodel import select

from app import acceptance_criteria as ac
from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME
from app.models import (
    ReleaseVersion,
    Alert,
    AlertSeverity,
    Artifact,
    ArtifactType,
    CompletionDebtEntry,
    ComputeJob,
    ComputeJobStatus,
    Direction,
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
    VersionAcceptanceCriterion,
    WakeAction,
    WakeDeliveryState,
    _utcnow,
)
from app.messages_last_seen import naive_utc_to_epoch_ms
from app.shared_state import runtime_config
from app.strategic_resident_session import (
    RESIDENT_SESSION_KEY,
    STATUS_ACTIVE,
    STRATEGIC_SESSION_ENABLED_SETTING,
)
from app.workbench_state import (
    STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY,
    sanitize_strategic_session_snapshot,
    sanitize_workbench_state,
)
from app.workbench_events import SURFACE_MESSAGES, SURFACE_RUNTIME, SURFACE_STRATEGIC_SESSION
from app.work_session_command_execution import OPERATOR_COMMAND_EVENT_TYPE
from app.work_session_metrics import WORK_SESSION_METRIC_REGISTRY

TEST_USER_ID = "test-user-123"

EXPECTED_CURATED_METRIC_PROVENANCE = {
    "attention.needs_operator": ("attention", "attention.counts.needs_operator"),
    "attention.watch": ("attention", "attention.counts.watch"),
    "messages.open": ("messages", "messages.counts.open"),
    "messages.linked": ("messages", "messages.counts.linked"),
    "tasks.running": ("tasks", "tasks.counts.running"),
    "tasks.blocked": ("tasks", "tasks.counts.blocked"),
    "tasks.parked": ("tasks", "tasks.counts.parked"),
    "tasks.failed": ("tasks", "tasks.counts.failed"),
    "runtime.compute_active": (
        "runtime",
        "runtime.work.compute_jobs.counts.active",
    ),
    "runtime.compute_failed": (
        "runtime",
        "runtime.work.compute_jobs.counts.failed_recent",
    ),
    "runtime.held_approvals": (
        "runtime",
        "runtime.work.held_approvals.count",
    ),
    "runtime.local_strategic_ready": (
        "runtime",
        "runtime.local_runner.eligible_live_count",
    ),
    "runtime.local_runner_holds": (
        "runtime",
        "runtime.work.local_runner_holds.count",
    ),
    "runtime.parked_wakes": (
        "runtime",
        "runtime.work.parked_wakes.counts.active",
    ),
    "runtime.wake_problems": (
        "runtime",
        "runtime.work.parked_wakes.counts.problem",
    ),
    "runtime.compute_spend": (
        "runtime",
        "runtime.work.compute_jobs.spend.window_total_usd",
    ),
    "context.stale": ("context", "context_package.status"),
    "context.items": ("context", "context_package.item_count"),
}


def _assert_curated_metric_provenance(metrics: dict[str, dict]) -> None:
    assert set(metrics) == set(WORK_SESSION_METRIC_REGISTRY)
    assert set(EXPECTED_CURATED_METRIC_PROVENANCE).issubset(metrics)
    for metric_id, metric in metrics.items():
        provenance = metric.get("provenance")
        assert isinstance(provenance, dict), metric_id
        assert isinstance(provenance.get("surface"), str), metric_id
        assert isinstance(provenance.get("path"), str), metric_id
        definition = metric.get("definition")
        assert isinstance(definition, dict), metric_id
        assert definition.get("source") == provenance["surface"], metric_id
        assert definition.get("path") == provenance["path"], metric_id
        assert isinstance(definition.get("unit"), str), metric_id
        assert isinstance(definition.get("window"), dict), metric_id
        assert isinstance(definition["window"].get("type"), str), metric_id
        assert isinstance(definition.get("denominator"), dict), metric_id
        assert isinstance(definition["denominator"].get("kind"), str), metric_id
        assert definition.get("target") == metric.get("target"), metric_id
        assert isinstance(definition.get("stale_behavior"), str), metric_id
    for metric_id, (surface, path) in EXPECTED_CURATED_METRIC_PROVENANCE.items():
        provenance = metrics[metric_id]["provenance"]
        assert provenance["surface"] == surface
        assert provenance["path"] == path


def test_workbench_state_accepts_only_chat_and_continuous():
    """The interaction vocabulary is collapsed to chat + continuous; the
    sanitizer accepts those two and coerces any retired / unknown value to chat
    (never crashes on legacy data)."""
    assert sanitize_workbench_state({"interaction_mode": "chat"}, touch=False)[
        "interaction_mode"
    ] == "chat"
    assert sanitize_workbench_state({"interaction_mode": "continuous"}, touch=False)[
        "interaction_mode"
    ] == "continuous"
    # Retired modes tolerated on read -> coerced to chat.
    for legacy in ("check_in", "monitor", "totally_unknown"):
        assert sanitize_workbench_state({"interaction_mode": legacy}, touch=False)[
            "interaction_mode"
        ] == "chat"


def test_session_snapshot_coerces_retired_interaction_modes_to_chat():
    """The per-message snapshot sanitizer also coerces legacy check_in / monitor
    to chat so an old snapshot never surfaces a retired mode."""
    for legacy in ("check_in", "monitor"):
        snapshot = sanitize_strategic_session_snapshot({"interaction_mode": legacy})
        assert snapshot["interaction_mode"] == "chat"
    assert (
        sanitize_strategic_session_snapshot({"interaction_mode": "continuous"})[
            "interaction_mode"
        ]
        == "continuous"
    )


def test_mode_collapse_migration_rewrites_legacy_modes_to_chat():
    """The 0367 migration rewrites any persisted check_in / monitor workbench
    mode to chat in messagesnoiseprefs (JSONB), chained off 0366. DB-free shape
    check (per CLAUDE.md) — the applied transform is exercised by the DB suite."""
    import pathlib

    migration = (
        pathlib.Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0367_collapse_strategic_interaction_modes.py"
    ).read_text()
    assert 'revision = "0367"' in migration
    assert 'down_revision = "0366"' in migration
    assert "messagesnoiseprefs" in migration
    # Targets the nested workbench interaction_mode and sets it to "chat".
    assert "{_strategic_workbench,interaction_mode}" in migration
    assert '\'"chat"\'::jsonb' in migration
    # Filters exactly the two retired modes.
    assert "'check_in', 'monitor'" in migration


def test_strategic_session_snapshot_preserves_canonical_context_target_fields():
    snapshot = sanitize_strategic_session_snapshot(
        {
            "selected_ref_descriptors": [
                {
                    "kind": "knowledge_document",
                    "id": "knowledge:document-1",
                    "target": {
                        "surface": " knowledge ",
                        "project_id": " project-1 ",
                        "section": " notes ",
                        "task_id": "task-1",
                        "message_id": "message-1",
                        "direction_id": "direction-1",
                        "alert_id": "alert-1",
                        "action": "open " + ("x" * 220),
                        "document_id": "document-1",
                        "artifact_id": "artifact-1",
                        "runner_id": "runner-1",
                        "cycle_id": "cycle-1",
                        "secret": "drop-me",
                    },
                }
            ]
        }
    )

    assert snapshot["selected_ref_descriptors"][0]["target"] == {
        "surface": "knowledge",
        "project_id": "project-1",
        "section": "notes",
        "task_id": "task-1",
        "message_id": "message-1",
        "direction_id": "direction-1",
        "alert_id": "alert-1",
        "action": "open " + ("x" * 175),
        "document_id": "document-1",
        "artifact_id": "artifact-1",
        "runner_id": "runner-1",
        "cycle_id": "cycle-1",
    }


def _assert_generated_at_iso_utc(payload: dict) -> None:
    generated_at = payload.get("generated_at")
    assert isinstance(generated_at, str) and generated_at
    parsed = datetime.fromisoformat(generated_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def _resolve_dotted_path(root: dict, path: str):
    current = root
    for part in path.split("."):
        assert isinstance(current, dict), path
        assert part in current, path
        current = current[part]
    return current


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
async def test_pending_confirmations_surface_as_needs_operator_attention(db_session):
    """A pending_user_confirmation yields a needs_operator attention item.

    Exercises the operator_attention seam directly: each durable confirmation
    becomes one needs_operator item (with per-type lifecycle + accept/dismiss
    affordances) and feeds the pending_confirmations count.
    """
    from app.operator_attention import build_operator_attention

    project = Project(
        name="Pending confirmations",
        user_id=TEST_USER_ID,
        problem_model={
            "pending_user_confirmation": [
                {
                    "id": "p-cred",
                    "type": "credentials_needed",
                    "proposal": "Need an OpenAI API key to run the experiment.",
                    "created_at": "2026-07-14T00:00:00+00:00",
                },
                {
                    "id": "p-decide",
                    "type": "major_decision",
                    "proposal": "Ship the migration this cycle?",
                },
            ]
        },
    )
    db_session.add(project)
    await db_session.commit()
    db_session.add(ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner"))
    await db_session.commit()
    await db_session.refresh(project)

    attention = await build_operator_attention(db_session, project, TEST_USER_ID)

    assert attention["level"] == "needs_operator"
    assert attention["counts"]["pending_confirmations"] == 2
    assert attention["counts"]["needs_operator"] == 2

    items = {item["id"]: item for item in attention["items"]}
    cred = items["confirmation:p-cred"]
    assert cred["kind"] == "confirmation"
    assert cred["severity"] == "needs_operator"
    assert cred["reason"] == "confirmation_credentials_needed"
    assert cred["confirmation_type"] == "credentials_needed"
    assert cred["unread"] is True
    assert "OpenAI API key" in cred["detail"]
    assert cred["ref"]["id"] == "p-cred"
    # Lifecycle names the credential-available clear condition + inline affordances.
    assert (
        cred["lifecycle"]["clears_when"]
        == "confirmation_accepted_dismissed_or_credential_provided"
    )
    accept_action = next(a for a in cred["actions"] if a["kind"] == "accept")
    assert accept_action["path"].endswith(
        f"/projects/{project.id}/pending-confirm/p-cred/accept"
    )
    assert any(a["kind"] == "dismiss" for a in cred["actions"])

    decide = items["confirmation:p-decide"]
    assert decide["reason"] == "confirmation_major_decision"
    assert decide["lifecycle"]["clears_when"] == "confirmation_accepted_or_dismissed"


@pytest.mark.asyncio
async def test_pending_confirmations_malformed_model_does_not_raise(db_session):
    """A malformed pending_user_confirmation value must not raise or emit items."""
    from app.operator_attention import build_operator_attention

    project = Project(
        name="Malformed pending confirmations",
        user_id=TEST_USER_ID,
        problem_model={"pending_user_confirmation": "garbage"},
    )
    db_session.add(project)
    await db_session.commit()
    db_session.add(ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner"))
    await db_session.commit()
    await db_session.refresh(project)

    attention = await build_operator_attention(db_session, project, TEST_USER_ID)

    assert attention["counts"]["pending_confirmations"] == 0
    assert not any(item["kind"] == "confirmation" for item in attention["items"])


@pytest.mark.asyncio
async def test_strategic_state_returns_persistent_workbench_projection(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    # One durable pending confirmation — asserted both as the scalar read-model
    # summary count and as a needs_operator attention item below.
    pm = dict(project.problem_model or {})
    pm["pending_user_confirmation"] = [
        {
            "id": "p-1",
            "type": "major_decision",
            "proposal": "Keep chat as the main surface.",
        }
    ]
    project.problem_model = pm
    db_session.add(project)
    await db_session.commit()
    phase2_task = Task(
        project_id=project.id,
        title="Strategic review",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.IN_PROGRESS,
        strategic_cycle_id="sc-active",
        vm_runtime_cost_banked_db=1.25,
        estimated_cost_usd=1.25,
    )
    queued_task = Task(
        project_id=project.id,
        title="Queued follow-up",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        vm_runtime_cost_banked_db=0.75,
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
    # No resident session started for this project → null-safe projection.
    assert "resident_session" in data
    assert data["resident_session"] is None
    # The resident-session feature flag defaults off, so the read model reports
    # the Start affordance as unavailable (the UI hides it in that case).
    assert data["resident_session_available"] is False
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
    # The fixture carries one pending_user_confirmation (p-1), which now surfaces
    # as a needs_operator attention item alongside the scalar summary count.
    assert data["attention"]["level"] == "needs_operator"
    assert data["attention"]["counts"]["needs_operator"] == 1
    assert data["attention"]["counts"]["pending_confirmations"] == 1
    confirmation_item = next(
        item for item in data["attention"]["items"] if item["id"] == "confirmation:p-1"
    )
    assert confirmation_item["severity"] == "needs_operator"
    assert confirmation_item["kind"] == "confirmation"
    assert confirmation_item["reason"] == "confirmation_major_decision"
    assert confirmation_item["ref"]["id"] == "p-1"
    assert "Keep chat as the main surface." in confirmation_item["detail"]
    assert confirmation_item["lifecycle"]["operator_action"] == "answer_decision_or_dismiss"
    assert {a["kind"] for a in confirmation_item["actions"]} == {"accept", "dismiss"}
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
        # The pending confirmation surfaces as a needs_operator attention item,
        # which the context package now aggregates as an attention source.
        "attention": 1,
    }
    assert package["item_count"] == 15
    assert package["canvas"]["node_count"] >= 8
    assert package["canvas"]["edge_count"] >= 7
    assert package["canvas"]["nodes"]
    assert package["canvas"]["edges"]
    assert any(node["kind"] == "knowledge_document" for node in package["canvas"]["nodes"])
    assert any(node["kind"] == "artifact" for node in package["canvas"]["nodes"])
    assert package["resources"][0]["kind"] == "canvas"
    assert any(resource["kind"] == "knowledge_document" for resource in package["resources"])
    assert any(resource["kind"] == "artifact" for resource in package["resources"])
    descriptors = package["descriptors"]
    descriptor_kinds = {descriptor["kind"] for descriptor in descriptors}
    assert {"context_map", "metric_cards", "table", "markdown", "resource_detail"} <= descriptor_kinds
    assert all(descriptor["schema_version"] == 1 for descriptor in descriptors)
    assert descriptors[0]["id"] == package["canvas"]["id"]
    assert descriptors[0]["target"] == {
        "surface": "project",
        "section": "problem-model",
    }
    assert any(
        metric["label"] == "Tasks"
        for descriptor in descriptors
        if descriptor["kind"] == "metric_cards"
        for metric in descriptor["metrics"]
    )
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
async def test_strategic_state_surfaces_active_resident_session(
    client: AsyncClient, db_session
):
    """An active resident session in Project.vision is projected to the UI.

    The operator console polls /strategic-state and derives sessionActive from
    ``resident_session.status == 'active' && !resident_session.stop_requested``,
    so the read model must reflect the session-state blob written under
    ``Project.vision[RESIDENT_SESSION_KEY]``.
    """
    project = await _make_project(db_session)
    resident_task = Task(
        project_id=project.id,
        title="Resident strategic session",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(resident_task)
    await db_session.commit()
    await db_session.refresh(resident_task)

    started_at = (_utcnow() - timedelta(minutes=3)).isoformat()
    project.vision = {
        **(project.vision or {}),
        RESIDENT_SESSION_KEY: {
            "version": 1,
            "session_id": "ss-active-1234",
            "task_id": str(resident_task.id),
            "runner_id": "runner-abc",
            "operator_user_id": TEST_USER_ID,
            "status": STATUS_ACTIVE,
            "stop_requested": False,
            "started_at": started_at,
            "updated_at": started_at,
            "ended_at": None,
            "last_progress_at": None,
            "last_operator_interaction_at": started_at,
        },
    }
    db_session.add(project)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    resident = resp.json()["resident_session"]
    assert resident is not None
    assert resident["session_id"] == "ss-active-1234"
    assert resident["task_id"] == str(resident_task.id)
    assert resident["runner_id"] == "runner-abc"
    assert resident["status"] == STATUS_ACTIVE
    assert resident["stop_requested"] is False
    assert resident["started_at"] == started_at
    assert resident["last_operator_interaction_at"] == started_at
    # The bounded projection must not leak internal-only session fields.
    assert "operator_user_id" not in resident
    assert "version" not in resident


@pytest.mark.asyncio
async def test_strategic_state_surfaces_resident_runner_options(
    client: AsyncClient, db_session
):
    """The read model exposes ``resident_session_runners`` (the device-picker
    options) — one entry per enrolled runner with online / agent-ready / eligible
    flags — so the UI can render the auto-pick + override dropdown."""
    project = await _make_project(db_session)
    live = LocalRunner(
        project_id=project.id,
        token_hash="a" * 64,
        hostname="desk-live",
        status="active",
        last_seen_at=_utcnow(),
        capabilities={"task_types": "all", "agent_available": True},
    )
    offline = LocalRunner(
        project_id=project.id,
        token_hash="b" * 64,
        hostname="desk-offline",
        status="active",
        last_seen_at=_utcnow() - timedelta(hours=1),
        capabilities={"task_types": "all"},
    )
    db_session.add_all([live, offline])
    await db_session.commit()
    await db_session.refresh(live)
    await db_session.refresh(offline)

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    runners = resp.json()["resident_session_runners"]
    by_id = {r["id"]: r for r in runners}
    assert set(by_id) == {str(live.id), str(offline.id)}
    assert by_id[str(live.id)]["name"] == "desk-live"
    assert by_id[str(live.id)]["online"] is True
    assert by_id[str(live.id)]["eligible"] is True
    assert by_id[str(offline.id)]["online"] is False
    assert by_id[str(offline.id)]["eligible"] is False


@pytest.mark.asyncio
async def test_strategic_state_active_session_pinned_offline_reports_blocked(
    client: AsyncClient, db_session
):
    """Block-on-offline end-to-end: an ACTIVE resident session pinned to a device
    that is now offline reports ``resident_session_availability`` = blocked /
    ``runner_offline`` (the UI prompts a restart), while the raw
    ``resident_session`` blob still reads active."""
    project = await _make_project(db_session)
    offline_runner = LocalRunner(
        project_id=project.id,
        token_hash="c" * 64,
        hostname="desk-gone",
        status="active",
        last_seen_at=_utcnow() - timedelta(hours=1),
        capabilities={"task_types": "all"},
    )
    db_session.add(offline_runner)
    await db_session.commit()
    await db_session.refresh(offline_runner)

    resident_task = Task(
        project_id=project.id,
        title="Resident strategic session",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(resident_task)
    await db_session.commit()
    await db_session.refresh(resident_task)

    started_at = (_utcnow() - timedelta(minutes=3)).isoformat()
    project.vision = {
        **(project.vision or {}),
        RESIDENT_SESSION_KEY: {
            "version": 1,
            "session_id": "ss-offline-1",
            "task_id": str(resident_task.id),
            "runner_id": str(offline_runner.id),
            "operator_user_id": TEST_USER_ID,
            "status": STATUS_ACTIVE,
            "stop_requested": False,
            "started_at": started_at,
            "updated_at": started_at,
            "last_progress_at": (_utcnow() - timedelta(minutes=1)).isoformat(),
            "last_operator_interaction_at": started_at,
        },
    }
    db_session.add(project)
    await db_session.commit()

    original = runtime_config.get(STRATEGIC_SESSION_ENABLED_SETTING, False)
    try:
        runtime_config[STRATEGIC_SESSION_ENABLED_SETTING] = True
        resp = await client.get(
            f"/api/admin/projects/{project.id}/strategic-state"
        )
    finally:
        runtime_config[STRATEGIC_SESSION_ENABLED_SETTING] = original
    assert resp.status_code == 200
    data = resp.json()
    # Raw blob unchanged (still active) — no auto-migration, no blob mutation.
    assert data["resident_session"]["status"] == STATUS_ACTIVE
    # Availability derives the mid-session offline block for the UI.
    availability = data["resident_session_availability"]
    assert availability["state"] == "blocked"
    assert availability["reason"] == "runner_offline"


@pytest.mark.asyncio
async def test_strategic_state_surfaces_resident_session_availability_flag(
    client: AsyncClient, db_session
):
    """`resident_session_available` mirrors the ``strategic_session_enabled`` flag.

    The operator console gates the Start affordance on this boolean so the button
    is hidden when the feature is off (a click would otherwise 403 at the start
    endpoint). The default is off; toggling the runtime flag flips the field.
    """
    project = await _make_project(db_session)

    original = runtime_config.get(STRATEGIC_SESSION_ENABLED_SETTING, False)
    try:
        runtime_config[STRATEGIC_SESSION_ENABLED_SETTING] = False
        resp = await client.get(
            f"/api/admin/projects/{project.id}/strategic-state"
        )
        assert resp.status_code == 200
        assert resp.json()["resident_session_available"] is False

        runtime_config[STRATEGIC_SESSION_ENABLED_SETTING] = True
        resp = await client.get(
            f"/api/admin/projects/{project.id}/strategic-state"
        )
        assert resp.status_code == 200
        assert resp.json()["resident_session_available"] is True
    finally:
        runtime_config[STRATEGIC_SESSION_ENABLED_SETTING] = original


@pytest.mark.asyncio
async def test_strategic_liveness_returns_mode_and_session_without_heavy_state(
    client: AsyncClient, db_session
):
    """The lightweight liveness probe returns mode + resident-session fields and
    omits every heavy strategic-state segment (context package, runtime, events,
    fingerprints, attention, tasks, cost).
    """
    project = await _make_project(db_session)
    resident_task = Task(
        project_id=project.id,
        title="Resident strategic session",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(resident_task)
    await db_session.commit()
    await db_session.refresh(resident_task)

    started_at = (_utcnow() - timedelta(minutes=3)).isoformat()
    project.vision = {
        **(project.vision or {}),
        RESIDENT_SESSION_KEY: {
            "version": 1,
            "session_id": "ss-live-9",
            "task_id": str(resident_task.id),
            "runner_id": "runner-xyz",
            "operator_user_id": TEST_USER_ID,
            "status": STATUS_ACTIVE,
            "stop_requested": False,
            "started_at": started_at,
            "updated_at": started_at,
            "last_progress_at": None,
            "last_operator_interaction_at": started_at,
        },
    }
    db_session.add(project)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-liveness")
    assert resp.status_code == 200
    data = resp.json()
    assert data["project_id"] == str(project.id)
    # Mode from the workbench row (default "chat" absent an explicit selection).
    assert data["interaction_mode"] == "chat"
    # Resident-session liveness projection (mirrors the heavy endpoint's shape).
    resident = data["resident_session"]
    assert resident is not None
    assert resident["session_id"] == "ss-live-9"
    assert resident["status"] == STATUS_ACTIVE
    assert resident["stop_requested"] is False
    # Bounded projection: internal-only fields never leak.
    assert "operator_user_id" not in resident
    assert data["resident_session_availability"]["state"] is not None
    assert data["resident_session_runners"] == []
    # None of the heavy Phase-1/Phase-2 segments are assembled.
    for heavy_key in (
        "context_package",
        "runtime",
        "work_session",
        "attention",
        "tasks",
        "messages",
        "problem_model",
        "sha_freshness",
        "state_revision",
        "model_integrity",
    ):
        assert heavy_key not in data, f"heavy key {heavy_key!r} leaked into liveness probe"


@pytest.mark.asyncio
async def test_strategic_liveness_runner_options_and_availability(
    client: AsyncClient, db_session
):
    """The probe surfaces the same runner-picker options and availability verdict
    the heavy endpoint would, computed from the cheap helpers only.

    With no live runner and the feature enabled, availability is blocked/no_runner;
    the eligible-live count is derived from the runner options' ``eligible`` flags.
    """
    project = await _make_project(db_session)
    offline = LocalRunner(
        project_id=project.id,
        token_hash="d" * 64,
        hostname="desk-cold",
        status="active",
        last_seen_at=_utcnow() - timedelta(hours=1),
        capabilities={"task_types": "all"},
    )
    db_session.add(offline)
    await db_session.commit()
    await db_session.refresh(offline)

    original = runtime_config.get(STRATEGIC_SESSION_ENABLED_SETTING, False)
    try:
        runtime_config[STRATEGIC_SESSION_ENABLED_SETTING] = True
        resp = await client.get(
            f"/api/admin/projects/{project.id}/strategic-liveness"
        )
    finally:
        runtime_config[STRATEGIC_SESSION_ENABLED_SETTING] = original
    assert resp.status_code == 200
    data = resp.json()
    assert data["resident_session_available"] is True
    runners = data["resident_session_runners"]
    assert {r["id"] for r in runners} == {str(offline.id)}
    assert runners[0]["online"] is False
    assert runners[0]["eligible"] is False
    # No eligible live runner -> blocked / no_runner.
    availability = data["resident_session_availability"]
    assert availability["state"] == "blocked"
    assert availability["reason"] == "no_runner"


@pytest.mark.asyncio
async def test_strategic_state_cost_uses_estimated_cost_usd(
    client: AsyncClient, db_session
):
    """estimated_cost_usd is the source of truth for cumulative cost.

    Single-attempt completed tasks never bank their cost into
    vm_runtime_cost_banked_db (banking only runs on retry/requeue paths).
    The terminal-success path sets estimated_cost_usd to the full VM cost
    (banked + current-attempt), so summing that column captures all tasks.
    """
    project = await _make_project(db_session)
    # Single-attempt completed task: cost only in estimated_cost_usd (never banked).
    single_attempt_completed = Task(
        project_id=project.id,
        title="Single-attempt completed",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        estimated_cost_usd=1.5,
    )
    # Multi-attempt failed task: estimated_cost_usd holds the full cost;
    # vm_runtime_cost_banked_db holds only the banked portion from prior retries.
    multi_attempt_failed = Task(
        project_id=project.id,
        title="Multi-attempt failed",
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        estimated_cost_usd=0.75,
        vm_runtime_cost_banked_db=0.5,
    )
    # Archived task must be excluded regardless of its cost.
    archived_cost = Task(
        project_id=project.id,
        title="Archived task",
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        archived=True,
        estimated_cost_usd=10.0,
        vm_runtime_cost_banked_db=10.0,
    )
    # Active task with no prior attempts has estimated_cost_usd=NULL → contributes 0.
    no_cost_yet = Task(
        project_id=project.id,
        title="Active first attempt",
        payloads=[{"type": "create"}],
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add_all(
        [single_attempt_completed, multi_attempt_failed, archived_cost, no_cost_yet]
    )
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")

    assert resp.status_code == 200
    # 1.5 (single-attempt) + 0.75 (multi-attempt full cost) = 2.25; archived excluded.
    assert resp.json()["tasks"]["cumulative_estimated_cost_usd"] == pytest.approx(2.25)


@pytest.mark.asyncio
async def test_strategic_state_cost_uses_sql_aggregate_and_current_attempt(
    client: AsyncClient, db_session
):
    """Cost total uses SQL SUM(estimated_cost_usd) for non-archived tasks."""
    project = await _make_project(db_session)
    # Task 1: completed with estimated_cost_usd — covered by SQL aggregate
    completed = Task(
        project_id=project.id,
        title="Completed task",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        estimated_cost_usd=2.00,
    )
    # Task 2: failed with estimated_cost_usd — also covered by SQL aggregate
    failed = Task(
        project_id=project.id,
        title="Failed task",
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        estimated_cost_usd=1.50,
    )
    # Task 3: archived — excluded from SQL aggregate
    archived = Task(
        project_id=project.id,
        title="Archived cost",
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        archived=True,
        estimated_cost_usd=99.0,
    )
    # Task 4: active with no estimated_cost_usd — contributes 0
    active = Task(
        project_id=project.id,
        title="Active task",
        payloads=[{"type": "create"}],
        status=TaskStatus.IN_PROGRESS,
        estimated_cost_usd=None,
    )
    db_session.add_all([completed, failed, archived, active])
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    data = resp.json()
    cost = data["tasks"]["cumulative_estimated_cost_usd"]
    # SQL aggregate picks up 2.00 + 1.50; archived and active(NULL) excluded.
    assert cost == pytest.approx(3.50)


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
    listener_completed_task = Task(
        project_id=project.id,
        title="Listener-stamped completed task",
        payloads=[{"type": "create", "direction_id": "d-done"}],
        status=TaskStatus.COMPLETED,
        created_at=base_time + timedelta(days=3),
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
        listener_completed_task,
        *newer_routine_tasks,
    ])
    await db_session.commit()
    await db_session.refresh(listener_completed_task)

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
    assert approval_row["movement_explanation"] == "Waiting for operator approval."
    assert "approval" in approval_row["reasons"]

    friction = focus["buckets"]["friction"]
    assert friction["total"] == 1
    blocked_row = friction["items"][0]
    assert blocked_row["id"] == str(blocked_task.id)
    assert blocked_row["direction_id"] == "d-blocked"
    assert blocked_row["blocked_reason"] == "Needs local runner registration"
    assert blocked_row["movement_explanation"] == "Needs local runner registration"
    assert "blocked" in blocked_row["reasons"]

    queued = focus["buckets"]["queued"]
    assert queued["total"] == 221
    assert queued["has_more"] is True
    assert queued["items"][0]["id"] == str(old_queued_task.id)
    assert queued["items"][0]["direction_id"] == "d-queued-context"
    assert queued["items"][0]["movement_explanation"] == (
        "Queued for the next available pipeline slot."
    )
    assert all(row["id"] != str(approval_task.id) for row in queued["items"])

    completed = focus["buckets"]["recent_completed"]
    assert completed["total"] == 3
    assert completed["items"][0]["id"] == str(listener_completed_task.id)
    assert completed["items"][0]["completed_at"] == listener_completed_task.completed_at.isoformat()
    assert completed["items"][1]["id"] == str(recently_completed_task.id)
    assert completed["items"][2]["id"] == str(older_completed_task.id)
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
    runner_events = [
        item
        for item in data["work_session"]["events"]["items"]
        if item["resource"] == "local_runner"
    ]
    assert len(runner_events) == 1
    assert runner_events[0]["action"] == "selected"
    assert runner_events[0]["target"] == {
        "surface": "runtime",
        "section": "devices",
        "runner_id": str(runner.id),
    }
    assert runner_events[0]["data"]["health"]["is_live"] is True
    assert "token_hash" not in str(runner_events[0])


@pytest.mark.asyncio
async def test_strategic_state_reports_strategic_runner_candidates(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    runners = [
        LocalRunner(
            project_id=project.id,
            token_hash="c" * 64,
            hostname="eligible-desk",
            status="active",
            last_seen_at=_utcnow(),
            capabilities={
                "task_types": [
                    "design_doc_update",
                    "strategic_review",
                    {"secret": "drop"},
                ],
                "has_gpu": "false",
                "max_concurrent": "3",
                "runner_preset": "strategic_local",
                "workload": "strategic",
                "agent_available": "true",
                "active_task_ids": [],
            },
        ),
        LocalRunner(
            project_id=project.id,
            token_hash="d" * 64,
            hostname="busy-desk",
            status="active",
            last_seen_at=_utcnow(),
            capabilities={
                "task_types": ["design_doc_update", "strategic_review"],
                "has_gpu": False,
                "max_concurrent": 1,
                "runner_preset": "strategic_local",
                "workload": "strategic",
                "agent_available": True,
                "active_task_ids": ["existing-task"],
            },
        ),
        LocalRunner(
            project_id=project.id,
            token_hash="e" * 64,
            hostname="stale-desk",
            status="active",
            last_seen_at=_utcnow() - timedelta(minutes=10),
            capabilities={
                "task_types": ["design_doc_update", "strategic_review"],
                "has_gpu": False,
                "max_concurrent": 2,
                "runner_preset": "strategic_local",
                "workload": "strategic",
                "agent_available": True,
            },
        ),
        LocalRunner(
            project_id=project.id,
            token_hash="f" * 64,
            hostname="docs-only",
            status="active",
            last_seen_at=_utcnow(),
            capabilities={
                "task_types": ["design_doc_update"],
                "has_gpu": False,
                "max_concurrent": 2,
                "runner_preset": "strategic_local",
                "workload": "strategic",
                "agent_available": True,
            },
        ),
    ]
    db_session.add_all(runners)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200, resp.text
    local_runner = resp.json()["runtime"]["local_runner"]
    by_host = {runner["hostname"]: runner for runner in local_runner["runners"]}

    assert local_runner["strategic_runner_count"] == 4
    assert local_runner["eligible_live_count"] == 1
    assert set(by_host) == {"eligible-desk", "busy-desk", "stale-desk", "docs-only"}

    eligible = by_host["eligible-desk"]
    assert eligible["eligible_for_active_task"] is True
    assert eligible["blocked_reason"] is None
    assert eligible["capabilities"] == {
        "runner_preset": "strategic_local",
        "workload": "strategic",
        "max_concurrent": 3,
        "task_types": ["design_doc_update", "strategic_review"],
    }
    assert "active_task_ids" not in eligible["capabilities"]
    assert "has_gpu" not in eligible["capabilities"]
    assert "agent_available" not in eligible["capabilities"]
    assert eligible["health"]["agent_available"] is None

    busy = by_host["busy-desk"]
    assert busy["handles_active_task_type"] is True
    assert busy["eligible_for_active_task"] is False
    assert busy["blocked_reason"] == "at_capacity"
    assert busy["health"]["available_slots"] == 0

    stale = by_host["stale-desk"]
    assert stale["eligible_for_active_task"] is False
    assert stale["blocked_reason"] == "heartbeat_stale"
    assert stale["health"]["device_status"] == "stale"

    docs_only = by_host["docs-only"]
    assert docs_only["handles_active_task_type"] is False
    assert docs_only["eligible_for_active_task"] is False
    assert docs_only["blocked_reason"] == "capability_mismatch"

    commands = {entry["id"]: entry for entry in resp.json()["work_session"]["commands"]}
    eligible_runner_id = str(runners[0].id)
    prefer_command = commands[f"session.runner.prefer.{eligible_runner_id}"]
    assert prefer_command["target"] == {
        "surface": "runtime",
        "project_id": str(project.id),
        "section": "devices",
        "action": "prefer_runner",
        "runner_id": eligible_runner_id,
    }
    assert prefer_command["mutation"] == {
        "method": "POST",
        "path": f"/api/admin/projects/{project.id}/strategic-session",
        "body": {"workbench": {"preferred_runner_id": eligible_runner_id}},
    }
    assert f"session.runner.prefer.{runners[1].id}" not in commands
    assert f"session.runner.prefer.{runners[2].id}" not in commands
    assert f"session.runner.prefer.{runners[3].id}" not in commands


@pytest.mark.asyncio
async def test_local_runner_readiness_metric_uses_matching_denominator(
    client: AsyncClient, db_session
):
    """The local_strategic_ready metric denominator must equal readiness_capable_runner_count
    (runners that handle the active task type), not strategic_runner_count (all strategic
    runners). A runner that handles only some strategic task types cannot become eligible
    for the numerator and must not inflate the denominator.
    """
    project = await _make_project(db_session)
    # full-capable: handles both core strategic types → readiness-capable and live+eligible
    full_runner = LocalRunner(
        project_id=project.id,
        token_hash="r1" * 32,
        hostname="full-capable",
        status="active",
        last_seen_at=_utcnow(),
        capabilities={
            "task_types": ["design_doc_update", "strategic_review"],
            "max_concurrent": 2,
            "runner_preset": "strategic_local",
            "workload": "strategic",
            "agent_available": True,
        },
    )
    # partial-capable: handles only one core type → strategic but NOT readiness-capable
    partial_runner = LocalRunner(
        project_id=project.id,
        token_hash="r2" * 32,
        hostname="partial-capable",
        status="active",
        last_seen_at=_utcnow(),
        capabilities={
            "task_types": ["design_doc_update"],
            "max_concurrent": 2,
            "runner_preset": "strategic_local",
            "workload": "strategic",
            "agent_available": True,
        },
    )
    db_session.add_all([full_runner, partial_runner])
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    local_runner = data["runtime"]["local_runner"]

    # Both runners are strategic, only full_runner is readiness-capable
    assert local_runner["strategic_runner_count"] == 2
    assert local_runner["readiness_capable_runner_count"] == 1
    assert local_runner["eligible_live_count"] == 1

    # The metric denominator path must resolve to readiness_capable_runner_count (1),
    # not strategic_runner_count (2) — the partial-capable runner cannot be eligible
    # for the numerator and must not inflate the denominator.
    metrics = {entry["id"]: entry for entry in data["work_session"]["curated_metrics"]}
    ready_metric = metrics["runtime.local_strategic_ready"]
    assert ready_metric["value"] == 1
    denominator_path = ready_metric["definition"]["denominator"]["path"]
    assert denominator_path == "runtime.local_runner.readiness_capable_runner_count"
    assert _resolve_dotted_path(data, denominator_path) == 1


@pytest.mark.asyncio
async def test_strategic_state_counts_assigned_compute_jobs_in_runner_capacity(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    runner = LocalRunner(
        project_id=project.id,
        token_hash="g" * 64,
        hostname="assigned-desk",
        status="active",
        last_seen_at=_utcnow(),
        capabilities={
            "task_types": ["design_doc_update", "strategic_review"],
            "max_concurrent": 1,
            "runner_preset": "strategic_local",
            "workload": "strategic",
            "agent_available": True,
            "active_compute_job_ids": [],
        },
    )
    db_session.add(runner)
    await db_session.commit()
    await db_session.refresh(runner)
    db_session.add(
        ComputeJob(
            project_id=project.id,
            backend=RUNNER_SUBPROCESS_BACKEND_NAME,
            status=ComputeJobStatus.QUEUED,
            polling_metadata={"runner_target_id": str(runner.id)},
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session",
        json={
            "workbench": {
                "interaction_mode": "continuous",
                "preferred_runner_id": str(runner.id),
            }
        },
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    local_runner = data["runtime"]["local_runner"]
    assert local_runner["eligible_live_count"] == 0
    candidate = local_runner["runners"][0]
    assert candidate["id"] == str(runner.id)
    assert candidate["eligible_for_active_task"] is False
    assert candidate["blocked_reason"] == "at_capacity"
    assert candidate["health"]["active_compute_job_count"] == 1
    assert candidate["health"]["available_slots"] == 0
    actions = {entry["id"]: entry for entry in data["session"]["actions"]}
    runner_action = actions["runtime.local_strategic.continuation_runner"]
    assert runner_action["target"] == {
        "surface": "runtime",
        "section": "devices",
        "runner_id": str(runner.id),
    }
    assert runner_action["blocked_reason"] == "at_capacity"


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
async def test_strategic_state_does_not_report_queued_cloud_task_as_working(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    queued_task = Task(
        project_id=project.id,
        title="Strategic review queued for cloud VM",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.QUEUED,
        strategic_cycle_id="sc-cloud-pending",
        vm_instance_id="gce-strategic-pending",
    )
    db_session.add(queued_task)
    await db_session.commit()
    await db_session.refresh(queued_task)

    active_cycle = StrategicCycle(
        id="sc-cloud-pending",
        project_id=project.id,
        outcome="phase2_running",
        phase1_started_at=_utcnow() - timedelta(minutes=10),
        phase1_completed_at=_utcnow() - timedelta(minutes=5),
        phase2_started_at=_utcnow() - timedelta(minutes=5),
        phase2_task_id=queued_task.id,
    )
    db_session.add(active_cycle)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    data = resp.json()
    local_runner = data["runtime"]["local_runner"]

    assert data["strategic"]["active_task"]["id"] == str(queued_task.id)
    assert data["strategic"]["active_task"]["status"] == "QUEUED"
    assert local_runner["mode"] == "cloud"
    assert local_runner["status"] == "unavailable"
    assert local_runner["location"] == "cloud"
    assert local_runner["status_reason"] == "cloud_dispatch_pending"
    assert local_runner["fallback_reason"] == "cloud_dispatch_pending"
    assert (
        local_runner["active_task_runtime"]["vm_instance_id"]
        == "gce-strategic-pending"
    )
    assert data["work_session"]["status"] == "unavailable"
    assert data["work_session"]["location"] == "cloud"
    assert data["work_session"]["status_reason"] == "cloud_dispatch_pending"


@pytest.mark.asyncio
async def test_strategic_state_exposes_local_required_cloud_assignment_violation(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    phase2_task = Task(
        project_id=project.id,
        title="Strategic review incorrectly on cloud VM",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.IN_PROGRESS,
        strategic_cycle_id="sc-local-cloud",
        vm_instance_id="gce-strategic-violation",
        context={
            STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
            STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY: "workbench_continuous",
        },
    )
    db_session.add(phase2_task)
    await db_session.commit()
    await db_session.refresh(phase2_task)

    active_cycle = StrategicCycle(
        id="sc-local-cloud",
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

    assert local_runner["mode"] == "cloud"
    assert local_runner["status"] == "working"
    assert local_runner["location"] == "cloud"
    assert local_runner["status_reason"] == "local_required_cloud_dispatch"
    assert local_runner["fallback_reason"] == "local_required_cloud_dispatch"
    assert (
        local_runner["active_task_runtime"]["vm_instance_id"]
        == "gce-strategic-violation"
    )


@pytest.mark.asyncio
async def test_strategic_state_reports_queued_local_required_task_as_pending_local(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    awaiting_since = (_utcnow() - timedelta(minutes=3)).isoformat()
    queued_task = Task(
        project_id=project.id,
        title="Strategic review waiting for local runner",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.QUEUED,
        strategic_cycle_id="sc-local-required",
        vm_instance_id="agent-vm-pre-dispatch-placeholder",
        context={
            STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
            STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY: "workbench_continuous",
            STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY: awaiting_since,
        },
    )
    db_session.add(queued_task)
    await db_session.commit()
    await db_session.refresh(queued_task)

    active_cycle = StrategicCycle(
        id="sc-local-required",
        project_id=project.id,
        outcome="phase2_running",
        phase1_started_at=_utcnow() - timedelta(minutes=10),
        phase1_completed_at=_utcnow() - timedelta(minutes=5),
        phase2_started_at=_utcnow() - timedelta(minutes=5),
        phase2_task_id=queued_task.id,
    )
    db_session.add(active_cycle)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    data = resp.json()
    local_runner = data["runtime"]["local_runner"]

    assert data["strategic"]["active_task"]["id"] == str(queued_task.id)
    assert local_runner["mode"] == "pending_local"
    assert local_runner["status"] == "unavailable"
    assert local_runner["location"] == "locally"
    assert local_runner["status_reason"] == "no_runner"
    assert local_runner["fallback_reason"] == "no_runner"
    assert local_runner["active_task_requires_local"] is True
    assert local_runner["active_task_awaiting_local_runner_since"] == awaiting_since
    assert local_runner["active_task_runtime"]["vm_instance_id"] is None
    assert data["work_session"]["status"] == "unavailable"
    assert data["work_session"]["location"] == "locally"
    assert data["work_session"]["status_reason"] == "no_runner"


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
    data = resp.json()
    active = data["strategic"]["active_cycle"]
    assert active["id"] == "sc-stale"
    assert active["has_active_task"] is False
    assert active["stale"] is True
    assert data["strategic"]["active_task"] is None
    assert data["context_package"]["status"] == "stale"
    metrics = {entry["id"]: entry for entry in data["work_session"]["curated_metrics"]}
    assert metrics["context.stale"]["value"] == 1
    assert metrics["context.stale"]["tone"] == "warning"
    assert metrics["context.stale"]["target"] == {
        "surface": "project",
        "section": "problem-model",
    }
    _assert_curated_metric_provenance(metrics)
    assert metrics["context.stale"]["provenance"]["status"] == "stale"
    assert metrics["context.stale"]["provenance"]["transform"] == "equals:stale"
    for metric in metrics.values():
        assert _resolve_dotted_path(data, metric["provenance"]["path"]) is not None
        definition = metric["definition"]
        for metadata_key in ("window", "denominator"):
            metadata_path = (definition.get(metadata_key) or {}).get("path")
            if metadata_path:
                assert _resolve_dotted_path(data, metadata_path) is not None


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
                    "interaction_mode": "continuous",
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

    assert workbench["interaction_mode"] == "continuous"
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
    assert work_session["mode"] == "continuous"
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
    _assert_curated_metric_provenance(metrics)
    assert metrics["context.items"]["provenance"]["status"] == resp.json()[
        "context_package"
    ]["status"]


@pytest.mark.asyncio
async def test_strategic_state_resolves_selected_refs_absent_from_context_package(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    now = _utcnow()
    source_task = Task(
        project_id=project.id,
        title="Older durable context source",
        payloads=[{"type": "research"}],
        status=TaskStatus.COMPLETED,
        latest_status_message="Finished durable context output.",
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    selected_message = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="user",
        content="This old blocker should stay navigable." + (" extra" * 80),
        status="linked",
        context={
            "strategic_session": {
                "selected_refs": [
                    {"kind": "direction", "id": "direction:d-older-kept"}
                ]
            }
        },
        created_at=now - timedelta(days=10),
    )
    selected_direction = Direction(
        project_id=project.id,
        direction_key="d-older-kept",
        text="Keep the older selected direction navigable",
        status="paused",
        priority="medium",
        data={"id": "d-older-kept"},
    )
    selected_doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=source_task.id,
        source_kind=SourceKind.DESIGN.value,
        title="Older selected knowledge",
        summary="Older knowledge should be resolved without loading the body.",
        body="DO NOT EMBED OLD KNOWLEDGE BODY " + ("body " * 200),
        topic="older-context",
        direction_id="d-older-kept",
        tags=[],
        source_citations=[],
        forward_quality_verdict={"passed": True},
        lifecycle_status="consumed",
        created_at=now - timedelta(days=10),
        updated_at=now - timedelta(days=10),
    )
    selected_artifact = Artifact(
        task_id=source_task.id,
        artifact_type=ArtifactType.INVESTIGATION_REPORT,
        storage_uri="gs://blueprint-test/older-report.md",
        logical_key="older-selected-report",
        logs="DO NOT EMBED OLD ARTIFACT LOGS " + ("logs " * 200),
        created_at=now - timedelta(days=10),
    )
    db_session.add(selected_message)
    db_session.add(selected_direction)
    db_session.add(selected_doc)
    db_session.add(selected_artifact)
    await db_session.commit()
    await db_session.refresh(selected_message)
    await db_session.refresh(selected_doc)
    await db_session.refresh(selected_artifact)

    for index in range(5):
        newer_task = Task(
            project_id=project.id,
            title=f"Newer context source {index}",
            payloads=[{"type": "research"}],
            status=TaskStatus.COMPLETED,
        )
        db_session.add(newer_task)
        await db_session.flush()
        db_session.add(
            KnowledgeDocument(
                project_id=project.id,
                source_task_id=newer_task.id,
                source_kind=SourceKind.DESIGN.value,
                title=f"Newer knowledge {index}",
                summary="newer",
                body="body",
                tags=[],
                source_citations=[],
                forward_quality_verdict={"passed": True},
                created_at=now - timedelta(minutes=index),
                updated_at=now - timedelta(minutes=index),
            )
        )
        db_session.add(
            Artifact(
                task_id=newer_task.id,
                artifact_type=ArtifactType.INVESTIGATION_REPORT,
                storage_uri=f"gs://blueprint-test/newer-report-{index}.md",
                logical_key=f"newer-report-{index}",
                created_at=now - timedelta(minutes=index),
            )
        )
    await db_session.commit()

    db_session.add(
        MessagesNoisePrefs(
            user_id=TEST_USER_ID,
            project_id=project.id,
            prefs={
                "_strategic_workbench": {
                    "version": 1,
                    "interaction_mode": "check_in",
                    "active_surface": "strategic",
                    "selected_refs": [
                        {"kind": "message", "id": f"message:{selected_message.id}"},
                        {"kind": "direction", "id": "direction:d-older-kept"},
                        {
                            "kind": "knowledge_document",
                            "id": f"knowledge:{selected_doc.id}",
                        },
                        {"kind": "artifact", "id": f"artifact:{selected_artifact.id}"},
                        {
                            "kind": "message",
                            "id": f"message:{uuid.uuid4()}",
                            "label": "Missing message",
                        },
                    ],
                }
            },
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200, resp.text
    context_package = resp.json()["context_package"]
    evidence = context_package["evidence"]
    assert str(selected_doc.id) not in {
        item["id"] for item in evidence["knowledge"]["items"]
    }
    assert str(selected_artifact.id) not in {
        item["id"] for item in evidence["artifacts"]["items"]
    }

    descriptors = {
        (entry["kind"], entry["id"]): entry
        for entry in resp.json()["work_session"]["selected_ref_descriptors"]
    }
    message_descriptor = descriptors[("message", f"message:{selected_message.id}")]
    assert message_descriptor["label"] == "BLOCKER from user"
    assert "summary" not in message_descriptor
    assert message_descriptor["status"] == "linked"
    assert message_descriptor["target"] == {
        "surface": "messages",
        "message_id": str(selected_message.id),
    }
    direction_descriptor = descriptors[("direction", "direction:d-older-kept")]
    assert (
        direction_descriptor["label"]
        == "Keep the older selected direction navigable"
    )
    assert direction_descriptor["summary"] == "medium"
    assert direction_descriptor["status"] == "paused"
    assert direction_descriptor["target"] == {
        "surface": "project",
        "section": "directions",
        "direction_id": "d-older-kept",
    }
    knowledge_descriptor = descriptors[
        ("knowledge_document", f"knowledge:{selected_doc.id}")
    ]
    assert knowledge_descriptor["label"] == "Older selected knowledge"
    assert (
        knowledge_descriptor["summary"]
        == "Older knowledge should be resolved without loading the body."
    )
    assert knowledge_descriptor["status"] == SourceKind.DESIGN.value
    assert knowledge_descriptor["target"]["document_id"] == str(selected_doc.id)
    assert "DO NOT EMBED" not in str(knowledge_descriptor)
    artifact_descriptor = descriptors[("artifact", f"artifact:{selected_artifact.id}")]
    assert artifact_descriptor["label"] == "older-selected-report"
    assert artifact_descriptor["summary"] == "Older durable context source"
    assert artifact_descriptor["status"] == ArtifactType.INVESTIGATION_REPORT.value
    assert artifact_descriptor["target"] == {
        "surface": "task",
        "task_id": str(source_task.id),
        "artifact_id": str(selected_artifact.id),
    }
    missing_descriptor = next(
        entry
        for entry in resp.json()["work_session"]["selected_ref_descriptors"]
        if entry["label"] == "Missing message"
    )
    assert missing_descriptor == {
        "kind": "message",
        "id": missing_descriptor["id"],
        "label": "Missing message",
    }
    preview_message = next(
        item
        for item in resp.json()["messages"]["items"]
        if item["id"] == str(selected_message.id)
    )
    preview_descriptors = preview_message["context"]["strategic_session"][
        "selected_ref_descriptors"
    ]
    assert preview_descriptors == [
        {
            "kind": "direction",
            "id": "direction:d-older-kept",
            "label": "Keep the older selected direction navigable",
            "summary": "medium",
            "status": "paused",
            "target": {
                "surface": "project",
                "section": "directions",
                "direction_id": "d-older-kept",
            },
        }
    ]


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
async def test_strategic_session_conversation_resolves_message_selected_refs(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    direction = Direction(
        project_id=project.id,
        direction_key="d-conversation-kept",
        text="Keep conversation context navigable",
        status="active",
        priority="high",
        data={"id": "d-conversation-kept"},
    )
    message = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="user",
        content="Use the older direction context in this session.",
        context={
            "strategic_session": {
                "selected_refs": [
                    {"kind": "direction", "id": "direction:d-conversation-kept"}
                ]
            }
        },
    )
    db_session.add_all([direction, message])
    await db_session.commit()
    await db_session.refresh(message)

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-session")

    assert resp.status_code == 200, resp.text
    conversation_message = next(
        item
        for item in resp.json()["conversation"]["items"]
        if item["id"] == str(message.id)
    )
    assert conversation_message["context"]["strategic_session"][
        "selected_ref_descriptors"
    ] == [
        {
            "kind": "direction",
            "id": "direction:d-conversation-kept",
            "label": "Keep conversation context navigable",
            "summary": "high",
            "status": "active",
            "target": {
                "surface": "project",
                "section": "directions",
                "direction_id": "d-conversation-kept",
            },
        }
    ]


@pytest.mark.asyncio
async def test_strategic_conversation_endpoint_returns_conversation_envelope(
    client: AsyncClient, db_session
):
    """GET /strategic-conversation wraps the transcript in {conversation: ...}."""
    project = await _make_project(db_session)
    base_time = _utcnow() - timedelta(minutes=10)
    messages = [
        ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="user" if i % 2 == 0 else "strategic",
            content=f"conv row {i:02d}",
            created_at=base_time + timedelta(minutes=i),
        )
        for i in range(3)
    ]
    db_session.add_all(messages)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-conversation")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "conversation" in body
    conversation = body["conversation"]
    assert "items" in conversation
    contents = [item["content"] for item in conversation["items"]]
    assert contents == ["conv row 00", "conv row 01", "conv row 02"]


@pytest.mark.asyncio
async def test_strategic_conversation_endpoint_enforces_member_access(
    client: AsyncClient, db_session
):
    """GET /strategic-conversation returns 404 when the caller is not a project member."""
    # Create a project owned by a different user so TEST_USER_ID has no membership
    other_project = Project(
        name="Other Project",
        user_id="other-user-999",
    )
    db_session.add(other_project)
    await db_session.commit()
    await db_session.refresh(other_project)

    resp = await client.get(
        f"/api/admin/projects/{other_project.id}/strategic-conversation"
    )

    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_strategic_conversation_endpoint_passes_include_message_id(
    client: AsyncClient, db_session
):
    """include_message_id= on the lightweight endpoint surfaces an older message."""
    project = await _make_project(db_session)
    base_time = _utcnow() - timedelta(hours=2)
    older = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="strategic",
        content="older targeted message",
        created_at=base_time,
    )
    newer_messages = [
        ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="user" if i % 2 == 0 else "strategic",
            content=f"newer row {i:02d}",
            created_at=base_time + timedelta(minutes=i + 1),
        )
        for i in range(5)
    ]
    db_session.add_all([older, *newer_messages])
    await db_session.commit()
    await db_session.refresh(older)

    resp = await client.get(
        f"/api/admin/projects/{project.id}/strategic-conversation"
        f"?include_message_id={older.id}"
    )

    assert resp.status_code == 200, resp.text
    ids = [item["id"] for item in resp.json()["conversation"]["items"]]
    assert ids[0] == str(older.id)


@pytest.mark.asyncio
async def test_strategic_conversation_etag_revalidates_and_binds_pinned_message(
    client: AsyncClient, db_session
):
    """A transcript ETag 304s only for the exact pinned transcript requested."""
    project = await _make_project(db_session)
    base_time = _utcnow() - timedelta(hours=2)
    older = ProjectMessage(
        project_id=project.id, message_type=MessageType.NOTE,
        from_type="strategic", content="pinned older", created_at=base_time,
    )
    recent_messages = [
        ProjectMessage(
            project_id=project.id, message_type=MessageType.NOTE,
            from_type="strategic", content=f"recent {i}",
            created_at=base_time + timedelta(minutes=i + 1),
        )
        for i in range(25)
    ]
    db_session.add_all([older, *recent_messages])
    await db_session.commit()

    unpinned = await client.get(f"/api/admin/projects/{project.id}/strategic-conversation")
    assert unpinned.status_code == 200
    etag = unpinned.headers.get("ETag")
    assert etag and etag.startswith('"')

    unchanged = await client.get(
        f"/api/admin/projects/{project.id}/strategic-conversation",
        headers={"If-None-Match": etag},
    )
    assert unchanged.status_code == 304
    assert unchanged.headers.get("ETag") == etag
    assert not unchanged.content

    pinned = await client.get(
        f"/api/admin/projects/{project.id}/strategic-conversation?include_message_id={older.id}",
        headers={"If-None-Match": etag},
    )
    assert pinned.status_code == 200
    assert pinned.headers.get("ETag") != etag
    assert pinned.json()["conversation"]["items"][0]["id"] == str(older.id)

    # The same pin continues to validate while its selected, out-of-window
    # message is unchanged, but changing that message must invalidate it.
    pinned_etag = pinned.headers["ETag"]
    pinned_unchanged = await client.get(
        f"/api/admin/projects/{project.id}/strategic-conversation?include_message_id={older.id}",
        headers={"If-None-Match": pinned_etag},
    )
    assert pinned_unchanged.status_code == 304
    older.content = "pinned older, edited in place"
    db_session.add(older)
    await db_session.commit()
    pinned_changed = await client.get(
        f"/api/admin/projects/{project.id}/strategic-conversation?include_message_id={older.id}",
        headers={"If-None-Match": pinned_etag},
    )
    assert pinned_changed.status_code == 200
    assert pinned_changed.json()["conversation"]["items"][0]["content"] == "pinned older, edited in place"

    # A pin for an already-visible message changes request context even though
    # it produces the same rendered item list as the unpinned request.
    visible = recent_messages[-1]
    unpinned_after_edit = await client.get(
        f"/api/admin/projects/{project.id}/strategic-conversation"
    )
    visible_pin = await client.get(
        f"/api/admin/projects/{project.id}/strategic-conversation?include_message_id={visible.id}",
        headers={"If-None-Match": unpinned_after_edit.headers["ETag"]},
    )
    assert visible_pin.status_code == 200
    visible_etag = visible_pin.headers["ETag"]
    visible_unchanged = await client.get(
        f"/api/admin/projects/{project.id}/strategic-conversation?include_message_id={visible.id}",
        headers={"If-None-Match": visible_etag},
    )
    assert visible_unchanged.status_code == 304


@pytest.mark.asyncio
async def test_strategic_session_create_persists_project_session(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)

    with patch("app.workbench_events.broadcast_to_project", new=AsyncMock()) as bcast:
        resp = await client.post(
            f"/api/admin/projects/{project.id}/strategic-session",
            json={
                "workbench": {
                    "interaction_mode": "continuous",
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
    bcast.assert_awaited_once()
    assert bcast.await_args.args[:2] == (str(project.id), "workbench_refetch")
    event_payload = bcast.await_args.args[2]
    assert event_payload["resource"] == "strategic_workbench"
    assert event_payload["action"] == "updated"
    assert event_payload["refetch"] == {
        "surfaces": [SURFACE_STRATEGIC_SESSION, SURFACE_RUNTIME],
    }
    assert event_payload["workbench"]["interaction_mode"] == "continuous"
    assert event_payload["workbench"]["active_surface"] == "project"
    assert event_payload["workbench"]["arming"] == "steering"
    data = resp.json()
    assert data["arming"] == "steering"
    assert data["session"]["mode"] == "continuous"
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
    assert stored.prefs["_strategic_workbench"]["interaction_mode"] == "continuous"
    await db_session.refresh(project)
    marker = (project.vision or {})["_strategic_refresh"]
    assert marker["reason"] == "workbench_interaction_mode"
    assert marker["origin"] == "operator"


@pytest.mark.asyncio
async def test_strategic_session_command_executes_mode_and_records_receipt(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)

    with patch(
        "app.routers.admin_project_strategic_session.emit_workbench_refetch",
        new=AsyncMock(),
    ) as bcast:
        resp = await client.post(
            f"/api/admin/projects/{project.id}/strategic-session/commands/session.mode.continuous/execute",
            json={"idempotency_key": "cmd-mode-continuous-1"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["command"]["status"] == "succeeded"
    assert data["command"]["applied"] is True
    assert data["command"]["receipt"]["command_id"] == "session.mode.continuous"
    assert data["command"]["receipt"]["result"]["arming"] == "steering"
    assert "_arm_after_receipt" not in data["command"]["receipt"]["result"]
    assert data["workbench"]["interaction_mode"] == "continuous"
    assert data["session"]["mode"] == "continuous"
    assert data["session"]["jobs"]["items"][0]["command_id"] == "session.mode.continuous"

    bcast.assert_awaited_once()
    assert bcast.await_args.args[0] == project.id
    assert bcast.await_args.kwargs["resource"] == "operator_command"
    assert bcast.await_args.kwargs["action"] == "succeeded"
    assert bcast.await_args.kwargs["summary"]["command_id"] == "session.mode.continuous"
    assert bcast.await_args.kwargs["refetch_surfaces"] == [
        SURFACE_STRATEGIC_SESSION,
        SURFACE_RUNTIME,
    ]

    duplicate = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session/commands/session.mode.continuous/execute",
        json={"idempotency_key": "cmd-mode-continuous-1"},
    )

    assert duplicate.status_code == 200, duplicate.text
    duplicate_command = duplicate.json()["command"]
    assert duplicate_command["status"] == "duplicate"
    assert duplicate_command["applied"] is False
    assert duplicate_command["receipt"]["id"] == data["command"]["receipt"]["id"]

    receipts = (
        await db_session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.context["event_type"].as_string()
                == "operator_command",
            )
        )
    ).scalars().all()
    assert len(receipts) == 1
    assert receipts[0].idempotency_key == "cmd-mode-continuous-1"


@pytest.mark.asyncio
async def test_strategic_session_command_rejects_disabled_and_navigation_commands(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)

    disabled = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session/commands/session.mode.chat/execute",
        json={"idempotency_key": "cmd-disabled-chat-1"},
    )
    navigation = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session/commands/context.canvas.open/execute",
        json={"idempotency_key": "cmd-navigation-1"},
    )

    assert disabled.status_code == 409, disabled.text
    disabled_body = disabled.json()
    assert (
        disabled_body.get("detail")
        or (disabled_body.get("error") or {}).get("message")
    ) == "Command is disabled"
    assert navigation.status_code in {404, 409}, navigation.text


@pytest.mark.asyncio
async def test_strategic_session_command_executes_attention_housekeeping(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    message_created_at = _utcnow() - timedelta(minutes=10)
    message = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="strategic",
        content="Need operator input.",
        status="open",
        created_at=message_created_at,
    )
    alert = Alert(
        project_id=project.id,
        severity=AlertSeverity.WARNING,
        title="Runner offline",
        message="The local runner stopped heartbeating.",
        domain="runner",
        event_type="local_runner_stale",
    )
    db_session.add_all([message, alert])
    await db_session.commit()
    await db_session.refresh(message)
    await db_session.refresh(alert)
    observed_at = naive_utc_to_epoch_ms(message_created_at + timedelta(minutes=5))

    seen_resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session/commands/attention.messages.mark_seen/execute",
        json={
            "idempotency_key": "cmd-mark-seen-1",
            "observed_at": observed_at,
        },
    )
    dismiss_resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session/commands/attention.alert.dismiss:{alert.id}/execute",
        json={"idempotency_key": "cmd-dismiss-alert-1"},
    )
    dismiss_duplicate_resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session/commands/attention.alert.dismiss:{alert.id}/execute",
        json={"idempotency_key": "cmd-dismiss-alert-1"},
    )

    assert seen_resp.status_code == 200, seen_resp.text
    assert seen_resp.json()["command"]["receipt"]["command_id"] == "attention.messages.mark_seen"
    last_seen = (
        await db_session.execute(
            select(MessagesLastSeen).where(
                MessagesLastSeen.user_id == TEST_USER_ID,
                MessagesLastSeen.project_id == project.id,
            )
        )
    ).scalar_one()
    assert last_seen.last_seen_at is not None
    assert naive_utc_to_epoch_ms(last_seen.last_seen_at) == observed_at

    assert dismiss_resp.status_code == 200, dismiss_resp.text
    assert dismiss_resp.json()["command"]["receipt"]["command_id"] == (
        f"attention.alert.dismiss:{alert.id}"
    )
    await db_session.refresh(alert)
    assert alert.dismissed_at is not None
    assert dismiss_duplicate_resp.status_code == 200, dismiss_duplicate_resp.text
    duplicate_command = dismiss_duplicate_resp.json()["command"]
    assert duplicate_command["status"] == "duplicate"
    assert duplicate_command["applied"] is False
    assert duplicate_command["receipt"]["id"] == dismiss_resp.json()["command"]["receipt"]["id"]

    state_resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert state_resp.status_code == 200, state_resp.text
    attention = state_resp.json()["attention"]
    assert attention["counts"]["unread"] == 0
    assert attention["counts"]["questions"] == 1
    assert attention["counts"]["alerts"] == 0
    assert attention["counts"]["warning_alerts"] == 0
    assert attention["level"] == "needs_operator"
    attention_items = {item["id"]: item for item in attention["items"]}
    message_item = attention_items[f"message:{message.id}"]
    assert message_item["unread"] is False
    assert message_item["reason"] == "question"
    assert message_item["lifecycle"]["acknowledge_effect"] == "clears_unread_only"
    assert f"alert:{alert.id}" not in attention_items


@pytest.mark.asyncio
async def test_strategic_session_command_failed_receipt_refetches(
    client: AsyncClient,
    db_session,
):
    project = await _make_project(db_session)
    alert = Alert(
        project_id=project.id,
        severity=AlertSeverity.WARNING,
        title="Runner offline",
        message="The local runner stopped heartbeating.",
        domain="runner",
        event_type="local_runner_stale",
    )
    db_session.add(alert)
    await db_session.commit()
    await db_session.refresh(alert)

    with (
        patch(
            "app.work_session_command_execution._execute_alert_dismiss",
            new=AsyncMock(
                side_effect=HTTPException(
                    status_code=503,
                    detail="Alert dismiss handler unavailable",
                )
            ),
        ),
        patch(
            "app.routers.admin_project_strategic_session.emit_workbench_refetch",
            new=AsyncMock(),
        ) as bcast,
    ):
        resp = await client.post(
            f"/api/admin/projects/{project.id}/strategic-session/commands/"
            f"attention.alert.dismiss:{alert.id}/execute",
            json={"idempotency_key": "cmd-dismiss-alert-failed-1"},
        )

    assert resp.status_code == 200, resp.text
    command = resp.json()["command"]
    assert command["status"] == "failed"
    assert command["applied"] is False
    assert command["receipt"]["command_id"] == f"attention.alert.dismiss:{alert.id}"
    assert command["receipt"]["status"] == "failed"
    assert command["receipt"]["result"] == {
        "kind": "command_failed",
        "status_code": 503,
        "error": "Alert dismiss handler unavailable",
    }
    assert resp.json()["session"]["jobs"]["items"][0]["status"] == "failed"

    bcast.assert_awaited_once()
    assert bcast.await_args.args[0] == project.id
    assert bcast.await_args.kwargs["resource"] == "operator_command"
    assert bcast.await_args.kwargs["action"] == "failed"
    assert bcast.await_args.kwargs["summary"]["status"] == "failed"
    assert bcast.await_args.kwargs["summary"]["receipt_id"] == command["receipt"]["id"]
    assert bcast.await_args.kwargs["message_id"] == command["receipt"]["id"]

    stored = await db_session.get(ProjectMessage, uuid.UUID(command["receipt"]["id"]))
    assert stored is not None
    assert stored.context["operator_command"]["status"] == "failed"
    assert stored.idempotency_key == "cmd-dismiss-alert-failed-1"


@pytest.mark.asyncio
async def test_strategic_session_continuous_writes_local_continuation_marker(
    client: AsyncClient, db_session
):
    from app.workbench_state import (
        STRATEGIC_LOCAL_CONTINUATION_KEY,
        STRATEGIC_LOCAL_CONTINUATION_TTL_SECONDS,
    )

    project = await _make_project(db_session)

    resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session",
        json={
            "workbench": {
                "interaction_mode": "continuous",
                "active_surface": "runtime",
            }
        },
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    continuation = data["runtime"]["local_runner"]["continuation"]
    assert continuation["enabled"] is True
    assert continuation["fresh"] is True
    assert continuation["preferred_runner_id"] is None
    assert continuation["reason"] == "no_runner"
    assert continuation["ttl_seconds"] == STRATEGIC_LOCAL_CONTINUATION_TTL_SECONDS
    assert continuation["owner_user_id"] == TEST_USER_ID
    assert continuation["owned_by_current_operator"] is True
    assert continuation["ownership"] == "current_operator"
    assert (
        continuation["owner_session_started_at"]
        == data["workbench"]["session_started_at"]
    )
    assert continuation["expires_at"]
    assert data["session"]["runtime"]["continuation"] == continuation
    assert data["status"] == "unavailable"
    assert data["status_reason"] == "no_runner"
    metrics = {entry["id"]: entry for entry in data["session"]["curated_metrics"]}
    assert metrics["runtime.local_strategic_ready"]["value"] == 0
    assert metrics["runtime.local_strategic_ready"]["tone"] == "warning"
    actions = {entry["id"]: entry for entry in data["session"]["actions"]}
    runner_action = actions["runtime.local_strategic.continuation_runner"]
    assert runner_action["label"] == "Check Workday runner"
    assert runner_action["priority"] == "high"
    assert runner_action["target"] == {
        "surface": "runtime",
        "section": "devices",
    }
    assert runner_action["blocked_reason"] == "no_runner"

    await db_session.refresh(project)
    marker = (project.vision or {})[STRATEGIC_LOCAL_CONTINUATION_KEY]
    assert marker["active"] is True
    assert marker["operator_user_id"] == TEST_USER_ID
    assert (
        marker["owner_session_started_at"]
        == data["workbench"]["session_started_at"]
    )
    assert marker["last_seen_at"]
    assert marker["updated_at"]


@pytest.mark.asyncio
async def test_strategic_session_continuous_preserves_preferred_runner_marker(
    client: AsyncClient, db_session
):
    from app.workbench_state import STRATEGIC_LOCAL_CONTINUATION_KEY

    project = await _make_project(db_session)
    runner = LocalRunner(
        project_id=project.id,
        token_hash="p" * 64,
        hostname="preferred-offline",
        status="active",
        capabilities={
            "task_types": ["design_doc_update", "strategic_review"],
            "runner_preset": "strategic_local",
            "workload": "strategic",
            "agent_available": True,
        },
    )
    db_session.add(runner)
    await db_session.commit()
    await db_session.refresh(runner)
    preferred_runner_id = str(runner.id)

    resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session",
        json={
            "workbench": {
                "interaction_mode": "continuous",
                "active_surface": "runtime",
                "preferred_runner_id": preferred_runner_id,
            }
        },
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["workbench"]["preferred_runner_id"] == preferred_runner_id
    continuation = data["runtime"]["local_runner"]["continuation"]
    assert continuation["preferred_runner_id"] == preferred_runner_id
    assert continuation["reason"] == "never_seen"

    await db_session.refresh(project)
    marker = (project.vision or {})[STRATEGIC_LOCAL_CONTINUATION_KEY]
    assert marker["preferred_runner_id"] == preferred_runner_id


@pytest.mark.asyncio
async def test_strategic_session_continuous_drops_unknown_preferred_runner(
    client: AsyncClient, db_session
):
    from app.workbench_state import STRATEGIC_LOCAL_CONTINUATION_KEY

    project = await _make_project(db_session)

    resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session",
        json={
            "workbench": {
                "interaction_mode": "continuous",
                "active_surface": "runtime",
                "preferred_runner_id": str(uuid.uuid4()),
            }
        },
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "preferred_runner_id" not in data["workbench"]
    continuation = data["runtime"]["local_runner"]["continuation"]
    assert continuation["preferred_runner_id"] is None

    await db_session.refresh(project)
    marker = (project.vision or {})[STRATEGIC_LOCAL_CONTINUATION_KEY]
    assert "preferred_runner_id" not in marker


@pytest.mark.asyncio
async def test_strategic_session_continuous_drops_non_strategic_preferred_runner(
    client: AsyncClient, db_session
):
    from app.workbench_state import STRATEGIC_LOCAL_CONTINUATION_KEY

    project = await _make_project(db_session)
    runner = LocalRunner(
        project_id=project.id,
        token_hash="n" * 64,
        hostname="docs-only",
        status="active",
        last_seen_at=_utcnow(),
        capabilities={
            "task_types": ["docs"],
            "max_concurrent": 1,
            "agent_available": True,
        },
    )
    db_session.add(runner)
    await db_session.commit()
    await db_session.refresh(runner)

    resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session",
        json={
            "workbench": {
                "interaction_mode": "continuous",
                "active_surface": "runtime",
                "preferred_runner_id": str(runner.id),
            }
        },
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "preferred_runner_id" not in data["workbench"]
    continuation = data["runtime"]["local_runner"]["continuation"]
    assert continuation["preferred_runner_id"] is None

    await db_session.refresh(project)
    marker = (project.vision or {})[STRATEGIC_LOCAL_CONTINUATION_KEY]
    assert "preferred_runner_id" not in marker


@pytest.mark.asyncio
async def test_strategic_session_continuous_omits_runner_action_when_ready(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    runner = LocalRunner(
        project_id=project.id,
        token_hash="r" * 64,
        hostname="ready-desk",
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

    resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session",
        json={
            "workbench": {
                "interaction_mode": "continuous",
                "preferred_runner_id": str(runner.id),
            }
        },
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["runtime"]["local_runner"]["mode"] == "local_ready"
    assert (
        data["runtime"]["local_runner"]["continuation"]["reason"]
        == "preferred_runner_live"
    )
    assert data["runtime"]["local_runner"]["runners"][0]["id"] == str(runner.id)
    assert data["runtime"]["local_runner"]["runners"][0]["is_preferred"] is True
    assert data["runtime"]["local_runner"]["runners"][0]["eligible_for_active_task"] is True
    metrics = {entry["id"]: entry for entry in data["session"]["curated_metrics"]}
    assert metrics["runtime.local_strategic_ready"]["value"] == 1
    assert metrics["runtime.local_strategic_ready"]["tone"] == "active"
    actions = {entry["id"]: entry for entry in data["session"]["actions"]}
    assert "runtime.local_strategic.continuation_runner" not in actions


@pytest.mark.asyncio
async def test_strategic_session_leaving_continuous_clears_local_continuation_marker(
    client: AsyncClient, db_session
):
    from app.workbench_state import STRATEGIC_LOCAL_CONTINUATION_KEY

    project = await _make_project(db_session)

    first = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session",
        json={"workbench": {"interaction_mode": "continuous"}},
    )
    second = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session",
        json={"workbench": {"interaction_mode": "check_in"}},
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    continuation = second.json()["runtime"]["local_runner"]["continuation"]
    assert continuation["enabled"] is False
    assert continuation["fresh"] is False
    assert continuation["reason"] == "not_enabled"

    await db_session.refresh(project)
    assert STRATEGIC_LOCAL_CONTINUATION_KEY not in (project.vision or {})


@pytest.mark.asyncio
async def test_strategic_state_reads_stale_local_continuation_marker_as_not_fresh(
    client: AsyncClient, db_session
):
    from app.workbench_state import (
        STRATEGIC_LOCAL_CONTINUATION_KEY,
        STRATEGIC_LOCAL_CONTINUATION_TTL_SECONDS,
    )

    project = await _make_project(db_session)
    stale_at = (
        _utcnow() - timedelta(seconds=STRATEGIC_LOCAL_CONTINUATION_TTL_SECONDS + 60)
    ).isoformat()
    vision = dict(project.vision or {})
    vision[STRATEGIC_LOCAL_CONTINUATION_KEY] = {
        "version": 1,
        "active": True,
        "operator_user_id": TEST_USER_ID,
        "updated_at": stale_at,
        "last_seen_at": stale_at,
    }
    project.vision = vision
    db_session.add(project)
    await db_session.commit()

    state_resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    session_resp = await client.get(
        f"/api/admin/projects/{project.id}/strategic-session"
    )

    assert state_resp.status_code == 200, state_resp.text
    assert session_resp.status_code == 200, session_resp.text
    continuation = state_resp.json()["runtime"]["local_runner"]["continuation"]
    assert continuation["enabled"] is True
    assert continuation["fresh"] is False
    assert continuation["reason"] == "stale"
    assert continuation["last_seen_at"] == stale_at
    assert continuation["owner_user_id"] == TEST_USER_ID
    assert continuation["owned_by_current_operator"] is True
    assert continuation["ownership"] == "current_operator"
    assert continuation["expires_at"]
    assert (
        session_resp.json()["session"]["runtime"]["continuation"] == continuation
    )


@pytest.mark.asyncio
async def test_strategic_state_preserves_fresh_foreign_local_continuation_marker(
    client: AsyncClient, db_session
):
    from app.workbench_state import STRATEGIC_LOCAL_CONTINUATION_KEY

    project = await _make_project(db_session)
    other_operator = "other-operator"
    other_session_started_at = (_utcnow() - timedelta(hours=1)).isoformat()
    fresh_at = (_utcnow() - timedelta(minutes=20)).isoformat()
    project.vision = {
        **(project.vision or {}),
        STRATEGIC_LOCAL_CONTINUATION_KEY: {
            "version": 1,
            "active": True,
            "operator_user_id": other_operator,
            "owner_session_started_at": other_session_started_at,
            "updated_at": fresh_at,
            "last_seen_at": fresh_at,
        },
    }
    db_session.add(project)
    await db_session.commit()

    state_resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    leave_resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session",
        json={"workbench": {"interaction_mode": "check_in"}},
    )
    enter_resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session",
        json={"workbench": {"interaction_mode": "continuous"}},
    )

    assert state_resp.status_code == 200, state_resp.text
    continuation = state_resp.json()["runtime"]["local_runner"]["continuation"]
    assert continuation["enabled"] is True
    assert continuation["fresh"] is True
    assert continuation["owner_user_id"] is None
    assert continuation["owner_session_started_at"] == other_session_started_at
    assert continuation["owned_by_current_operator"] is False
    assert continuation["ownership"] == "other_operator"
    assert continuation["expires_at"]

    assert leave_resp.status_code == 200, leave_resp.text
    leave_continuation = leave_resp.json()["runtime"]["local_runner"]["continuation"]
    assert leave_continuation["owner_user_id"] is None
    assert leave_continuation["ownership"] == "other_operator"
    assert enter_resp.status_code == 200, enter_resp.text
    enter_continuation = enter_resp.json()["runtime"]["local_runner"]["continuation"]
    assert enter_resp.json()["workbench"]["interaction_mode"] == "continuous"
    assert enter_continuation["owner_user_id"] is None
    assert enter_continuation["ownership"] == "other_operator"
    await db_session.refresh(project)
    marker = (project.vision or {})[STRATEGIC_LOCAL_CONTINUATION_KEY]
    assert marker["operator_user_id"] == other_operator
    assert marker["owner_session_started_at"] == other_session_started_at
    assert marker["last_seen_at"] == fresh_at


@pytest.mark.asyncio
async def test_strategic_session_stale_duplicate_cannot_replace_newer_marker(
    client: AsyncClient, db_session
):
    from app.workbench_state import STRATEGIC_LOCAL_CONTINUATION_KEY

    project = await _make_project(db_session)
    old_session_started_at = (_utcnow() - timedelta(hours=2)).isoformat()
    newer_session_started_at = (_utcnow() - timedelta(minutes=10)).isoformat()
    fresh_at = (_utcnow() - timedelta(minutes=5)).isoformat()
    project.vision = {
        **(project.vision or {}),
        STRATEGIC_LOCAL_CONTINUATION_KEY: {
            "version": 1,
            "active": True,
            "operator_user_id": TEST_USER_ID,
            "owner_session_started_at": newer_session_started_at,
            "updated_at": fresh_at,
            "last_seen_at": fresh_at,
        },
    }
    db_session.add(project)
    await db_session.commit()

    leave_resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session",
        json={
            "workbench": {
                "interaction_mode": "check_in",
                "session_started_at": old_session_started_at,
            }
        },
    )
    reenter_resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session",
        json={
            "workbench": {
                "interaction_mode": "continuous",
                "session_started_at": old_session_started_at,
            }
        },
    )

    assert leave_resp.status_code == 200, leave_resp.text
    leave_continuation = leave_resp.json()["runtime"]["local_runner"]["continuation"]
    assert leave_continuation["owner_user_id"] == TEST_USER_ID
    assert leave_continuation["owner_session_started_at"] == newer_session_started_at
    assert reenter_resp.status_code == 200, reenter_resp.text
    reenter_continuation = reenter_resp.json()["runtime"]["local_runner"][
        "continuation"
    ]
    assert reenter_continuation["owner_user_id"] == TEST_USER_ID
    assert reenter_continuation["owner_session_started_at"] == newer_session_started_at

    await db_session.refresh(project)
    marker = (project.vision or {})[STRATEGIC_LOCAL_CONTINUATION_KEY]
    assert marker["operator_user_id"] == TEST_USER_ID
    assert marker["owner_session_started_at"] == newer_session_started_at
    assert marker["last_seen_at"] == fresh_at


@pytest.mark.asyncio
async def test_strategic_session_malformed_owner_lease_does_not_pin_marker(
    client: AsyncClient, db_session
):
    from app.workbench_state import STRATEGIC_LOCAL_CONTINUATION_KEY

    project = await _make_project(db_session)
    fresh_at = (_utcnow() - timedelta(minutes=5)).isoformat()
    project.vision = {
        **(project.vision or {}),
        STRATEGIC_LOCAL_CONTINUATION_KEY: {
            "version": 1,
            "active": True,
            "operator_user_id": TEST_USER_ID,
            "owner_session_started_at": "not-a-timestamp",
            "updated_at": fresh_at,
            "last_seen_at": fresh_at,
        },
    }
    db_session.add(project)
    await db_session.commit()

    session_started_at = (_utcnow() - timedelta(minutes=1)).isoformat()
    resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session",
        json={
            "workbench": {
                "interaction_mode": "continuous",
                "session_started_at": session_started_at,
            }
        },
    )

    assert resp.status_code == 200, resp.text
    continuation = resp.json()["runtime"]["local_runner"]["continuation"]
    assert continuation["owner_user_id"] == TEST_USER_ID
    assert continuation["owner_session_started_at"] == session_started_at
    await db_session.refresh(project)
    marker = (project.vision or {})[STRATEGIC_LOCAL_CONTINUATION_KEY]
    assert marker["owner_session_started_at"] == session_started_at
    assert marker["last_seen_at"] != fresh_at


@pytest.mark.asyncio
async def test_strategic_session_message_refreshes_local_continuation_marker(
    client: AsyncClient, db_session
):
    from app.workbench_state import (
        STRATEGIC_LOCAL_CONTINUATION_KEY,
        STRATEGIC_LOCAL_CONTINUATION_TTL_SECONDS,
    )

    project = await _make_project(db_session)
    stale_at = (
        _utcnow() - timedelta(seconds=STRATEGIC_LOCAL_CONTINUATION_TTL_SECONDS + 60)
    ).isoformat()
    project.vision = {
        **(project.vision or {}),
        STRATEGIC_LOCAL_CONTINUATION_KEY: {
            "version": 1,
            "active": True,
            "operator_user_id": TEST_USER_ID,
            "updated_at": stale_at,
            "last_seen_at": stale_at,
        },
    }
    db_session.add(project)
    await db_session.commit()

    resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session/messages",
        json={
            "message_type": "FEEDBACK",
            "content": "Keep working locally while I continue this session.",
            "workbench_context": {
                "interaction_mode": "continuous",
                "active_surface": "strategic",
            },
        },
    )
    state_resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")

    assert resp.status_code == 200, resp.text
    assert state_resp.status_code == 200, state_resp.text
    data = resp.json()
    assert data["status"] == "created"
    assert data["session_status"] == "unavailable"
    continuation = data["runtime"]["local_runner"]["continuation"]
    assert continuation["enabled"] is True
    assert continuation["fresh"] is True
    assert continuation["last_seen_at"] != stale_at
    assert data["session"]["runtime"]["continuation"] == continuation
    assert state_resp.json()["runtime"]["local_runner"]["continuation"] == continuation

    await db_session.refresh(project)
    marker = (project.vision or {})[STRATEGIC_LOCAL_CONTINUATION_KEY]
    assert marker["operator_user_id"] == TEST_USER_ID
    assert marker["last_seen_at"] != stale_at


@pytest.mark.asyncio
async def test_strategic_session_message_records_operator_message_and_snapshot(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    await client.post(
        f"/api/admin/projects/{project.id}/strategic-session",
        json={
            "workbench": {
                "interaction_mode": "continuous",
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
        "workbench_context": {
            "interaction_mode": "continuous",
            "active_surface": "pipeline",
            "selected_refs": [
                {"kind": "task", "id": "task-9", "label": "Watch task"}
            ],
            "selected_ref_descriptors": [
                {
                    "kind": "runner",
                    "id": "runner-ready",
                    "label": "Local Strategic runner",
                    "target": {
                        "surface": "runtime",
                        "section": "devices",
                        "runner_id": "runner-ready",
                    },
                }
            ],
        },
    }
    with patch(
        "app.routers.admin_project_strategic_session.broadcast_to_project",
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
    broadcast.assert_awaited_once()
    assert broadcast.await_args.args[:2] == (str(project.id), "message_created")
    event_payload = broadcast.await_args.args[2]
    assert event_payload["schema_version"] == 1
    assert event_payload["project_id"] == str(project.id)
    assert event_payload["resource"] == "project_message"
    assert event_payload["action"] == "created"
    assert event_payload["refetch"] == {"surfaces": ["messages", "strategic_session"]}
    assert event_payload["message_id"] == first.json()["id"]
    assert event_payload["type"] == "FEEDBACK"
    assert event_payload["from"] == "user"
    assert event_payload["message"]["id"] == first.json()["id"]
    assert event_payload["message"]["status"] == "open"

    msg = await db_session.get(ProjectMessage, uuid.UUID(first.json()["id"]))
    assert msg.from_type == "user"
    assert msg.message_type == MessageType.FEEDBACK
    assert msg.idempotency_key == "strategic-session-message-1"
    session_snapshot = (msg.context or {})["strategic_session"]
    assert session_snapshot["interaction_mode"] == "continuous"
    assert session_snapshot["active_surface"] == "pipeline"
    assert session_snapshot["selected_refs"] == [
        {"kind": "task", "id": "task-9", "label": "Watch task"}
    ]
    assert session_snapshot["selected_ref_descriptors"][0]["target"] == {
        "surface": "runtime",
        "section": "devices",
        "runner_id": "runner-ready",
    }
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
    assert resp.json()["session"]["mode"] == "continuous"
    assert resp.json()["session_status"] == "unavailable"
    assert any(
        item["id"] == resp.json()["id"]
        for item in resp.json()["conversation"]["items"]
    )
    stored = await db_session.get(ProjectMessage, uuid.UUID(resp.json()["id"]))
    assert stored is not None
    stored_session = (stored.context or {})["strategic_session"]
    assert stored_session["context_package_id"] == f"strategic-context:{project.id}"
    assert stored_session["selected_ref_descriptors"] == descriptors
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
async def test_strategic_session_message_does_not_persist_selected_message_body(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    selected_message = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="user",
        content="SECRET MESSAGE BODY should not become hidden Strategic context.",
        status="open",
    )
    db_session.add(selected_message)
    await db_session.commit()
    await db_session.refresh(selected_message)

    resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session/messages",
        json={
            "message_type": "FEEDBACK",
            "content": "Use the selected message only as a navigable pointer.",
            "workbench_context": {
                "interaction_mode": "continuous",
                "active_surface": "messages",
                "selected_refs": [
                    {
                        "kind": "message",
                        "id": f"message:{selected_message.id}",
                    },
                ],
                "selected_ref_descriptors": [
                    {
                        "kind": "MESSAGE",
                        "id": f"message:{selected_message.id}",
                        "label": "Hostile descriptor",
                        "summary": "SECRET MESSAGE BODY from supplied context",
                        "status": "SECRET STATUS BODY",
                        "body": "SECRET BODY FIELD",
                        "logs": "SECRET LOG FIELD",
                        "target": {
                            "surface": "SECRET TARGET SURFACE",
                            "section": "SECRET TARGET SECTION",
                            "message_id": str(selected_message.id),
                            "logs": "SECRET TARGET LOG",
                        },
                    },
                ],
            },
        },
    )

    assert resp.status_code == 200, resp.text
    descriptor = resp.json()["message"]["context"]["strategic_session"][
        "selected_ref_descriptors"
    ][0]
    assert descriptor["kind"] == "message"
    assert descriptor["label"] == "BLOCKER from user"
    assert "summary" not in descriptor
    assert descriptor["target"] == {
        "surface": "messages",
        "message_id": str(selected_message.id),
    }

    stored = await db_session.get(ProjectMessage, uuid.UUID(resp.json()["id"]))
    assert stored is not None
    serialized = str((stored.context or {})["strategic_session"])
    assert "SECRET MESSAGE BODY" not in serialized
    assert "Hostile descriptor" not in serialized
    assert "SECRET STATUS BODY" not in serialized
    assert "SECRET BODY FIELD" not in serialized
    assert "SECRET LOG FIELD" not in serialized
    assert "SECRET TARGET SURFACE" not in serialized
    assert "SECRET TARGET SECTION" not in serialized
    assert "SECRET TARGET LOG" not in serialized
    stored_descriptor = (stored.context or {})["strategic_session"][
        "selected_ref_descriptors"
    ][0]
    assert stored_descriptor == {
        "kind": "message",
        "id": f"message:{selected_message.id}",
        "target": {
            "surface": "messages",
            "message_id": str(selected_message.id),
        },
    }


@pytest.mark.asyncio
async def test_strategic_session_message_response_survives_arming_rollback(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)

    async def _rollback_and_decline(session, *_args, **_kwargs):
        await session.rollback()
        return "not_armed"

    with patch(
        "app.project_message_creation.maybe_arm_operator_refresh",
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
                    "interaction_mode": "continuous",
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
    assert workbench["interaction_mode"] == "continuous"
    assert workbench["active_surface"] == "pipeline"
    assert workbench["selected_refs"] == [
        {"kind": "task", "id": "task-9", "label": "Watch task"}
    ]
    assert datetime.fromisoformat(workbench["last_interaction_at"]) > datetime.fromisoformat(old_interaction_at)


@pytest.mark.asyncio
async def test_strategic_state_marks_check_in_unavailable_without_prior_receipt(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)

    state_resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    session_resp = await client.get(f"/api/admin/projects/{project.id}/strategic-session")

    assert state_resp.status_code == 200, state_resp.text
    assert session_resp.status_code == 200, session_resp.text
    state_work_session = state_resp.json()["work_session"]
    session_work_session = session_resp.json()["session"]
    assert state_work_session["last_check_in"] is None
    assert state_work_session["since_last_check_in"]["available"] is False
    assert state_work_session["since_last_check_in"]["reason"] == "no_prior_check_in"
    assert session_work_session["last_check_in"] is None
    assert session_work_session["since_last_check_in"]["available"] is False
    assert session_work_session["commands"] == state_work_session["commands"]
    assert session_work_session["curated_metrics"] == state_work_session["curated_metrics"]
    metrics = {entry["id"]: entry for entry in state_work_session["curated_metrics"]}
    _assert_curated_metric_provenance(metrics)


@pytest.mark.asyncio
async def test_strategic_session_check_in_records_quiet_durable_receipt(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    original_refresh = dict((project.vision or {}).get("_strategic_refresh") or {})
    body = {
        "content": "Daily check-in: focus on release readiness.",
        "kind": "daily",
        "idempotency_key": "daily-check-in-2026-06-30",
        "workbench_context": {
            "interaction_mode": "continuous",
            "active_surface": "strategic",
            "selected_refs": [
                {"kind": "task", "id": "task-check", "label": "Release task"}
            ],
        },
    }

    with patch("app.workbench_events.broadcast_to_project", new=AsyncMock()) as bcast:
        first = await client.post(
            f"/api/admin/projects/{project.id}/strategic-session/check-in",
            json=body,
        )
        second = await client.post(
            f"/api/admin/projects/{project.id}/strategic-session/check-in",
            json=body,
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["check_in"]["status"] == "created"
    assert second.json()["check_in"]["status"] == "duplicate"
    assert second.json()["check_in"]["message_id"] == first.json()["check_in"]["message_id"]
    bcast.assert_awaited_once()
    assert bcast.await_args.args[:2] == (str(project.id), "workbench_refetch")
    event_payload = bcast.await_args.args[2]
    assert event_payload["resource"] == "strategic_check_in"
    assert event_payload["action"] == "created"
    assert event_payload["refetch"] == {
        "surfaces": [SURFACE_MESSAGES, SURFACE_STRATEGIC_SESSION],
    }
    assert event_payload["message_id"] == first.json()["check_in"]["message_id"]
    assert event_payload["check_in"]["message_id"] == first.json()["check_in"]["message_id"]

    message_id = uuid.UUID(first.json()["check_in"]["message_id"])
    msg = await db_session.get(ProjectMessage, message_id)
    assert msg is not None
    assert msg.from_type == "system"
    assert msg.status == "resolved"
    assert msg.message_type == MessageType.NOTE
    assert msg.content == "Daily check-in: focus on release readiness."
    assert msg.idempotency_key == "daily-check-in-2026-06-30"
    assert msg.context["event_type"] == "strategic_check_in"
    assert msg.context["check_in"]["operator_user_id"] == TEST_USER_ID
    assert msg.context["check_in"]["kind"] == "daily"
    assert msg.context["strategic_session"]["interaction_mode"] == "continuous"
    assert msg.context["strategic_session"]["selected_refs"] == [
        {"kind": "task", "id": "task-check", "label": "Release task"}
    ]

    prefs_row = (
        await db_session.execute(
            select(MessagesNoisePrefs).where(
                MessagesNoisePrefs.project_id == project.id,
                MessagesNoisePrefs.user_id == TEST_USER_ID,
            )
        )
    ).scalar_one()
    stored_workbench = (prefs_row.prefs or {})["_strategic_workbench"]
    assert stored_workbench["interaction_mode"] == "continuous"
    assert stored_workbench["active_surface"] == "strategic"
    assert stored_workbench["selected_refs"] == [
        {"kind": "task", "id": "task-check", "label": "Release task"}
    ]
    await db_session.refresh(project)
    assert (project.vision or {}).get("_strategic_refresh") == original_refresh

    data = first.json()
    assert data["session"]["last_check_in"]["message_id"] == str(message_id)
    assert data["session"]["last_check_in"]["kind"] == "daily"
    assert data["session"]["since_last_check_in"]["available"] is True
    assert all(item["id"] != str(message_id) for item in data["conversation"]["items"])
    assert all(item["id"] != str(message_id) for item in data["recent_outputs"])

    count = await db_session.scalar(
        select(func.count())
        .select_from(ProjectMessage)
        .where(ProjectMessage.project_id == project.id)
        .where(ProjectMessage.idempotency_key == "daily-check-in-2026-06-30")
    )
    assert count == 1


@pytest.mark.asyncio
async def test_strategic_session_check_in_continuous_mode_updates_local_marker_without_arming(
    client: AsyncClient, db_session
):
    from app.workbench_state import STRATEGIC_LOCAL_CONTINUATION_KEY

    project = await _make_project(db_session)
    original_refresh = dict((project.vision or {}).get("_strategic_refresh") or {})
    resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session/check-in",
        json={
            "content": "Keep the local Strategic session warm.",
            "idempotency_key": "check-in-continuous-local",
            "workbench_context": {
                "interaction_mode": "continuous",
                "active_surface": "runtime",
                "selected_refs": [
                    {
                        "kind": "task",
                        "id": "local-strategic-task",
                        "label": "Local Strategic task",
                    }
                ],
            },
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["check_in"]["status"] == "created"
    prefs_row = (
        await db_session.execute(
            select(MessagesNoisePrefs).where(
                MessagesNoisePrefs.project_id == project.id,
                MessagesNoisePrefs.user_id == TEST_USER_ID,
            )
        )
    ).scalar_one()
    stored_workbench = (prefs_row.prefs or {})["_strategic_workbench"]
    assert stored_workbench["interaction_mode"] == "continuous"
    assert stored_workbench["active_surface"] == "runtime"
    await db_session.refresh(project)
    vision = project.vision or {}
    marker = vision[STRATEGIC_LOCAL_CONTINUATION_KEY]
    assert marker["active"] is True
    assert marker["operator_user_id"] == TEST_USER_ID
    assert vision.get("_strategic_refresh") == original_refresh


@pytest.mark.asyncio
async def test_strategic_session_check_in_duplicate_does_not_replace_snapshot(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    future_task_id = uuid.uuid4()
    first = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session/check-in",
        json={
            "content": "First checkpoint.",
            "idempotency_key": "check-in-stable-snapshot",
            "workbench_context": {
                "interaction_mode": "continuous",
                "active_surface": "runtime",
                "selected_refs": [
                    {
                        "kind": "task",
                        "id": str(future_task_id),
                        "label": "First task",
                    }
                ],
                "selected_ref_descriptors": [
                    {
                        "kind": "task",
                        "id": str(future_task_id),
                        "label": "First task snapshot",
                        "summary": "Operator context before the task row existed.",
                    }
                ],
            },
        },
    )
    db_session.add(
        Task(
            id=future_task_id,
            project_id=project.id,
            title="Resolved task that appeared later",
            status=TaskStatus.IN_PROGRESS,
            latest_status_message="This must not rewrite the duplicate snapshot.",
        )
    )
    await db_session.commit()
    duplicate = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session/check-in",
        json={
            "content": "Replay with newer context.",
            "idempotency_key": "check-in-stable-snapshot",
            "workbench_context": {
                "interaction_mode": "monitor",
                "active_surface": "pipeline",
                "selected_refs": [
                    {"kind": "task", "id": "second-task", "label": "Second task"}
                ],
            },
        },
    )

    assert first.status_code == 200, first.text
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["check_in"]["status"] == "duplicate"
    message = await db_session.get(
        ProjectMessage,
        uuid.UUID(first.json()["check_in"]["message_id"]),
    )
    assert message is not None
    session_snapshot = (message.context or {})["strategic_session"]
    assert session_snapshot["interaction_mode"] == "continuous"
    assert session_snapshot["active_surface"] == "runtime"
    assert session_snapshot["selected_refs"] == [
        {"kind": "task", "id": str(future_task_id), "label": "First task"}
    ]
    assert session_snapshot["selected_ref_descriptors"] == [
        {
            "kind": "task",
            "id": str(future_task_id),
            "label": "First task snapshot",
            "summary": "Operator context before the task row existed.",
        }
    ]
    prefs_row = (
        await db_session.execute(
            select(MessagesNoisePrefs).where(
                MessagesNoisePrefs.project_id == project.id,
                MessagesNoisePrefs.user_id == TEST_USER_ID,
            )
        )
    ).scalar_one()
    assert (prefs_row.prefs or {})["_strategic_workbench"]["selected_refs"] == [
        {"kind": "task", "id": str(future_task_id), "label": "First task"}
    ]


@pytest.mark.asyncio
async def test_strategic_session_check_in_replaces_stale_selected_ref_descriptors(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    task = Task(
        project_id=project.id,
        title="Server resolved auth task",
        status=TaskStatus.IN_PROGRESS,
        latest_status_message="OAuth flow is active.",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session/check-in",
        json={
            "content": "Checkpoint with selected task.",
            "idempotency_key": "check-in-resolved-descriptor",
            "workbench_context": {
                "interaction_mode": "check_in",
                "active_surface": "pipeline",
                "selected_refs": [
                    {
                        "kind": "task",
                        "id": str(task.id),
                        "label": "Client stale task",
                    },
                    {
                        "kind": "task",
                        "id": "operator-only-task",
                        "label": "Fallback task",
                    }
                ],
                "selected_ref_descriptors": [
                    {
                        "kind": "task",
                        "id": str(task.id),
                        "label": "Stale client title",
                        "summary": "Stale client summary",
                        "status": "stale",
                    },
                    {
                        "kind": "task",
                        "id": "operator-only-task",
                        "label": "Operator-only task",
                        "summary": "Keep this sideboard note.",
                        "target": {
                            "surface": "project",
                            "section": "directions",
                        },
                    }
                ],
            },
        },
    )

    assert resp.status_code == 200, resp.text
    message = await db_session.get(
        ProjectMessage,
        uuid.UUID(resp.json()["check_in"]["message_id"]),
    )
    assert message is not None
    descriptors = (message.context or {})["strategic_session"][
        "selected_ref_descriptors"
    ]
    assert descriptors == [
        {
            "kind": "task",
            "id": str(task.id),
            "label": "Server resolved auth task",
            "summary": "OAuth flow is active.",
            "status": "IN_PROGRESS",
            "target": {"surface": "task", "task_id": str(task.id)},
        },
        {
            "kind": "task",
            "id": "operator-only-task",
            "label": "Operator-only task",
            "summary": "Keep this sideboard note.",
            "target": {
                "surface": "project",
                "section": "directions",
            },
        }
    ]


@pytest.mark.asyncio
async def test_strategic_session_check_in_duplicate_replay_does_not_move_marker_backward(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    old_resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session/check-in",
        json={
            "content": "Older check-in",
            "idempotency_key": "check-in-old",
        },
    )
    assert old_resp.status_code == 200, old_resp.text
    old_message = await db_session.get(
        ProjectMessage,
        uuid.UUID(old_resp.json()["check_in"]["message_id"]),
    )
    assert old_message is not None
    old_message.created_at = _utcnow() - timedelta(minutes=10)
    old_message.resolved_at = old_message.created_at
    db_session.add(old_message)
    await db_session.commit()

    new_resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session/check-in",
        json={
            "content": "Newer check-in",
            "idempotency_key": "check-in-new",
        },
    )
    replay_resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session/check-in",
        json={
            "content": "Older check-in replay",
            "idempotency_key": "check-in-old",
        },
    )
    state_resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")

    assert new_resp.status_code == 200, new_resp.text
    assert replay_resp.status_code == 200, replay_resp.text
    assert replay_resp.json()["check_in"]["status"] == "duplicate"
    assert state_resp.status_code == 200, state_resp.text
    assert (
        state_resp.json()["work_session"]["last_check_in"]["message_id"]
        == new_resp.json()["check_in"]["message_id"]
    )
    assert state_resp.json()["work_session"]["last_check_in"]["content"] == "Newer check-in"


@pytest.mark.asyncio
async def test_strategic_state_counts_changes_since_last_check_in(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    check_in_resp = await client.post(
        f"/api/admin/projects/{project.id}/strategic-session/check-in",
        json={"idempotency_key": "check-in-before-change-counts"},
    )
    assert check_in_resp.status_code == 200, check_in_resp.text
    check_in_message = await db_session.get(
        ProjectMessage,
        uuid.UUID(check_in_resp.json()["check_in"]["message_id"]),
    )
    assert check_in_message is not None
    check_in_message.created_at = _utcnow() - timedelta(minutes=10)
    check_in_message.resolved_at = check_in_message.created_at
    db_session.add(check_in_message)
    await db_session.commit()
    await db_session.refresh(check_in_message)
    since = check_in_message.created_at
    after = since + timedelta(minutes=1)
    before = since - timedelta(minutes=1)
    future = _utcnow() + timedelta(hours=1)

    db_session.add(
        ProjectMessage(
            project_id=project.id,
            message_type=MessageType.QUESTION,
            from_type="strategic",
            content="Should we ship the runner update today?",
            status="open",
            created_at=after,
        )
    )
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="system",
            content="Command completed: mark messages seen",
            status="resolved",
            created_at=after + timedelta(seconds=10),
            context={
                "event_type": OPERATOR_COMMAND_EVENT_TYPE,
                "operator_command": {
                    "command_id": "attention.messages.mark_seen",
                    "status": "succeeded",
                },
            },
        )
    )
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            message_type=MessageType.BLOCKER,
            from_type="strategic",
            content="This future blocker must not be counted yet.",
            status="open",
            created_at=future,
        )
    )
    db_session.add(
        Task(
            project_id=project.id,
            title="Blocked post-check-in task",
            status=TaskStatus.BLOCKED,
            created_at=before,
            updated_at=after,
        )
    )
    db_session.add(
        Task(
            project_id=project.id,
            title="Completed post-check-in task",
            status=TaskStatus.COMPLETED,
            created_at=before,
            updated_at=after,
            completed_at=after,
        )
    )
    db_session.add(
        Task(
            project_id=project.id,
            title="Failed post-check-in task",
            status=TaskStatus.FAILED,
            created_at=after,
            updated_at=after,
        )
    )
    db_session.add(
        Task(
            project_id=project.id,
            title="Future failed task",
            status=TaskStatus.FAILED,
            created_at=future,
            updated_at=future,
        )
    )
    db_session.add(
        Alert(
            project_id=project.id,
            severity=AlertSeverity.ERROR,
            title="Runner disconnected",
            message="The local runner missed its heartbeat.",
            created_at=after,
        )
    )
    db_session.add(
        Alert(
            project_id=project.id,
            severity=AlertSeverity.ERROR,
            title="Future alert",
            message="This alert is outside the check-in window.",
            created_at=future,
        )
    )
    db_session.add(
        StrategicCycle(
            id=str(uuid.uuid4()),
            project_id=project.id,
            outcome="completed",
            phase1_started_at=before,
            phase1_completed_at=after,
            phase2_started_at=after,
            phase2_completed_at=after,
            summary_text="Opened a release readiness direction.",
            directions_created=1,
            tasks_spawned=2,
        )
    )
    db_session.add(
        StrategicCycle(
            id=str(uuid.uuid4()),
            project_id=project.id,
            outcome="completed",
            phase1_started_at=future,
            phase1_completed_at=future,
            phase2_started_at=future,
            phase2_completed_at=future,
            summary_text="Future cycle should not be counted.",
        )
    )
    await db_session.commit()

    state_resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    session_resp = await client.get(f"/api/admin/projects/{project.id}/strategic-session")

    assert state_resp.status_code == 200, state_resp.text
    assert session_resp.status_code == 200, session_resp.text
    since_state = state_resp.json()["work_session"]["since_last_check_in"]
    assert since_state["available"] is True
    assert since_state["messages"]["total"] == 1
    assert since_state["messages"]["strategic"] == 1
    assert since_state["messages"]["needs_operator"] == 1
    assert since_state["tasks"]["created"] == 1
    assert since_state["tasks"]["updated"] == 3
    assert since_state["tasks"]["completed"] == 2
    assert since_state["tasks"]["failed"] == 1
    assert since_state["tasks"]["blocked"] == 1
    assert since_state["alerts"]["raised"] == 1
    assert since_state["alerts"]["errors"] == 1
    assert since_state["strategic"]["cycles_completed"] == 1
    assert since_state["strategic"]["latest_cycle"]["summary_text"] == "Opened a release readiness direction."
    session_since = session_resp.json()["session"]["since_last_check_in"]
    assert session_since["available"] is True
    assert session_since["since"] == since_state["since"]
    assert session_since["messages"] == since_state["messages"]
    assert session_since["tasks"] == since_state["tasks"]
    assert session_since["alerts"] == since_state["alerts"]
    assert session_since["strategic"] == since_state["strategic"]


@pytest.mark.asyncio
async def test_strategic_state_and_session_expose_work_session_event_feed(
    client: AsyncClient,
    db_session,
):
    project = await _make_project(db_session)
    now = _utcnow()
    message = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.QUESTION,
        from_type="strategic",
        content="Confirm the local runner policy.",
        status="open",
        created_at=now - timedelta(minutes=3),
    )
    task = Task(
        project_id=project.id,
        payloads=[{"type": "review"}],
        status=TaskStatus.IN_PROGRESS,
        title="Review runner readiness",
        latest_status_message="Checking the runner contract.",
        created_at=now - timedelta(minutes=8),
        updated_at=now - timedelta(minutes=2),
    )
    alert = Alert(
        project_id=project.id,
        severity=AlertSeverity.WARNING,
        title="Runner stale",
        message="The local runner has not checked in.",
        domain="runner",
        created_at=now - timedelta(minutes=1),
    )
    db_session.add(message)
    db_session.add(task)
    db_session.add(alert)
    await db_session.commit()

    state_resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    session_resp = await client.get(f"/api/admin/projects/{project.id}/strategic-session")

    assert state_resp.status_code == 200, state_resp.text
    assert session_resp.status_code == 200, session_resp.text
    state_events = state_resp.json()["work_session"]["events"]
    session_events = session_resp.json()["session"]["events"]
    assert session_events == state_events
    assert state_events["version"] == 1
    assert state_events["limit"] == 24
    assert state_events["has_more"] is False
    by_resource = {item["resource"]: item for item in state_events["items"]}
    assert set(by_resource) >= {"alert", "task", "project_message"}
    assert by_resource["alert"]["action"] == "raised"
    assert by_resource["alert"]["refetch"]["surfaces"] == [
        "runtime",
        "strategic_session",
    ]
    assert by_resource["task"]["action"] == "in_progress"
    assert by_resource["task"]["target"] == {
        "surface": "task",
        "task_id": str(task.id),
    }
    assert by_resource["project_message"]["target"] == {
        "surface": "messages",
        "message_id": str(message.id),
        "action": "reply",
    }


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
    event_items = data["work_session"]["events"]["items"]
    compute_events = [
        item for item in event_items if item["resource"] == "compute_job"
    ]
    assert {item["action"] for item in compute_events} == {"running", "failed"}
    running_event = next(item for item in compute_events if item["action"] == "running")
    assert running_event["target"] == {
        "surface": "runtime",
        "section": "compute",
        "task_id": str(active_task.id),
    }
    assert running_event["data"]["backend"] == "local"
    assert "next_poll_at" not in running_event["data"]
    assert "polling_metadata" not in running_event["data"]

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
    _assert_curated_metric_provenance(session_metrics)
    assert session_metrics["tasks.parked"]["value"] == 1
    assert session_metrics["runtime.compute_active"]["value"] == 1
    assert session_metrics["runtime.compute_active"]["target"] == {
        "surface": "runtime",
        "section": "compute",
    }
    assert "window_minutes" not in session_metrics["runtime.compute_active"][
        "provenance"
    ]
    assert session_metrics["runtime.compute_failed"]["value"] == 1
    assert session_metrics["runtime.compute_failed"]["tone"] == "critical"
    assert session_metrics["runtime.compute_failed"]["provenance"][
        "window_minutes"
    ] == 360
    assert session_metrics["runtime.held_approvals"]["value"] == 1
    assert session_metrics["runtime.held_approvals"]["target"] == {
        "surface": "pipeline",
    }
    assert "window_minutes" not in session_metrics["runtime.held_approvals"][
        "provenance"
    ]
    assert session_metrics["runtime.parked_wakes"]["value"] == 1
    assert session_metrics["runtime.wake_problems"]["value"] == 1
    assert session_metrics["runtime.compute_spend"]["unit"] == "USD"
    assert session_metrics["runtime.compute_spend"]["value"] == pytest.approx(3.75)
    assert session_metrics["runtime.compute_spend"]["provenance"][
        "window_minutes"
    ] == 360
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
    assert local_attention["movement_explanation"] == "No eligible local runner is online."
    assert local_attention["lifecycle"] == {
        "mode": "runtime_lifecycle",
        "clears_when": "eligible_runner_available_or_task_dispatches_expires_terminal_or_archived",
        "operator_action": "connect_or_select_local_runner",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    }

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
async def test_strategic_state_surfaces_strategic_local_runner_holds(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    awaiting_since = (_utcnow() - timedelta(minutes=20)).isoformat()
    strategic_hold = Task(
        project_id=project.id,
        title="Run Strategic locally",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.QUEUED,
        latest_status_message="Workday mode is waiting for a local runner.",
        context={
            STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
            STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY: awaiting_since,
        },
        priority=1,
    )
    missing_required_marker = Task(
        project_id=project.id,
        title="Malformed Strategic local marker",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.QUEUED,
        context={STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY: awaiting_since},
        priority=2,
    )
    wrong_type_marker = Task(
        project_id=project.id,
        title="Non-Strategic local marker",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        context={
            STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
            STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY: awaiting_since,
        },
        priority=3,
    )
    db_session.add_all([strategic_hold, missing_required_marker, wrong_type_marker])
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    data = resp.json()

    holds = data["runtime"]["work"]["local_runner_holds"]
    assert holds["count"] == 1
    assert holds["compute_count"] == 0
    assert holds["strategic_count"] == 1
    assert holds["items"][0]["id"] == str(strategic_hold.id)
    assert holds["items"][0]["awaiting_since"] == awaiting_since

    attention = data["attention"]
    assert attention["counts"]["local_runner_holds"] == 1
    assert attention["counts"]["strategic_local_runner_holds"] == 1
    assert attention["counts"]["needs_operator"] == 1
    item = next(
        item
        for item in attention["items"]
        if item["id"] == f"task:{strategic_hold.id}:local_runner_hold"
    )
    assert item["awaiting_since"] == awaiting_since
    assert item["target"] == {
        "surface": "runtime",
        "project_id": str(project.id),
        "section": "devices",
        "task_id": str(strategic_hold.id),
    }
    assert item["lifecycle"]["mode"] == "runtime_lifecycle"
    assert item["lifecycle"]["operator_action"] == "connect_or_select_local_runner"


@pytest.mark.asyncio
async def test_operator_attention_surfaces_stale_pipeline_intervention_tasks(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    now = _utcnow()
    stale_dep = str(uuid.uuid4())
    healthy_dependency = Task(
        project_id=project.id,
        title="Healthy upstream dependency",
        payloads=[{"type": "create"}],
        status=TaskStatus.IN_PROGRESS,
        created_at=now - timedelta(minutes=30),
    )
    stale_queued = Task(
        project_id=project.id,
        title="Queued behind stale dependency",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        depends_on=[stale_dep],
        created_at=now - timedelta(minutes=20),
        latest_status_message="Waiting on upstream foundation.",
        priority=1,
    )
    healthy_waiter = Task(
        project_id=project.id,
        title="Queued behind healthy dependency",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        depends_on=[str(healthy_dependency.id)],
        created_at=now - timedelta(minutes=20),
        priority=2,
    )
    fresh_queued = Task(
        project_id=project.id,
        title="Fresh queued dependency",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        depends_on=[str(uuid.uuid4())],
        created_at=now - timedelta(minutes=2),
    )
    old_no_dependency = Task(
        project_id=project.id,
        title="Old normal queue",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        created_at=now - timedelta(hours=1),
    )
    stale_provisioning = Task(
        project_id=project.id,
        title="Provisioning exceeded stale threshold",
        payloads=[{"type": "create", "scope": "small"}],
        status=TaskStatus.PROVISIONING,
        vm_instance_id="vm-stale-provision",
        vm_provisioned_at=now - timedelta(minutes=20),
        latest_status_message="VM was dispatched but never claimed.",
        priority=2,
    )
    fresh_provisioning = Task(
        project_id=project.id,
        title="Provisioning still within threshold",
        payloads=[{"type": "create", "scope": "small"}],
        status=TaskStatus.PROVISIONING,
        vm_instance_id="vm-fresh-provision",
        vm_provisioned_at=now - timedelta(minutes=1),
    )
    approval_provisioning = Task(
        project_id=project.id,
        title="Approval provisioning wins precedence",
        payloads=[{"type": "create", "scope": "small"}],
        status=TaskStatus.PROVISIONING,
        requires_approval=True,
        vm_instance_id="vm-approval-provision",
        vm_provisioned_at=now - timedelta(minutes=20),
    )
    stale_runner = LocalRunner(
        project_id=project.id,
        token_hash="hash-stale-runner",
        hostname="workstation",
        status="active",
        last_seen_at=now - timedelta(minutes=10),
    )
    live_runner = LocalRunner(
        project_id=project.id,
        token_hash="hash-live-runner",
        hostname="online-workstation",
        status="active",
        last_seen_at=now,
    )
    db_session.add_all(
        [
            stale_queued,
            healthy_dependency,
            healthy_waiter,
            fresh_queued,
            old_no_dependency,
            stale_provisioning,
            fresh_provisioning,
            approval_provisioning,
            stale_runner,
            live_runner,
        ]
    )
    await db_session.flush()
    stale_runner_pin = Task(
        project_id=project.id,
        title="Pinned to stale runner",
        payloads=[{"type": "create"}],
        status=TaskStatus.PROVISIONING,
        vm_instance_id=f"runner:{stale_runner.id}",
        vm_provisioned_at=now - timedelta(minutes=20),
        context={"runner_target_id": str(stale_runner.id)},
        latest_status_message="Waiting for the runner to pull work.",
        priority=3,
    )
    live_runner_pin = Task(
        project_id=project.id,
        title="Pinned to live runner",
        payloads=[{"type": "create"}],
        status=TaskStatus.PROVISIONING,
        vm_instance_id=f"runner:{live_runner.id}",
        vm_provisioned_at=now - timedelta(minutes=20),
        context={"runner_target_id": str(live_runner.id)},
    )
    db_session.add_all([stale_runner_pin, live_runner_pin])
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    data = resp.json()
    attention = data["attention"]

    assert attention["level"] == "needs_operator"
    assert attention["counts"]["stale_queued_tasks"] == 1
    assert attention["counts"]["stale_provisioning_tasks"] == 1
    assert attention["counts"]["stale_runner_pins"] == 1
    assert attention["counts"]["approvals"] == 1
    assert attention["counts"]["needs_operator"] == 4
    assert attention["counts"]["watch"] == 0

    items = {item["id"]: item for item in attention["items"]}
    queued_item = items[f"task:{stale_queued.id}:stale_queued_task"]
    assert queued_item["label"] == "Dependency queue stale"
    assert queued_item["detail"] == "Waiting on upstream foundation."
    assert queued_item["lifecycle"]["clears_when"] == (
        "dependency_recovers_task_leaves_queue_terminal_or_archived"
    )
    assert queued_item["lifecycle"]["operator_action"] == (
        "inspect_dependency_or_requeue_task"
    )
    assert queued_item["target"] == {
        "surface": "task",
        "project_id": str(project.id),
        "task_id": str(stale_queued.id),
    }
    assert f"task:{fresh_queued.id}:stale_queued_task" not in items
    assert f"task:{old_no_dependency.id}:stale_queued_task" not in items
    assert f"task:{healthy_waiter.id}:stale_queued_task" not in items

    provisioning_item = items[
        f"task:{stale_provisioning.id}:stale_provisioning_task"
    ]
    assert provisioning_item["label"] == "Provisioning stale"
    assert provisioning_item["detail"] == "VM was dispatched but never claimed."
    assert provisioning_item["lifecycle"]["clears_when"] == (
        "task_claims_healthy_runtime_recovers_terminal_or_archived"
    )
    assert provisioning_item["lifecycle"]["operator_action"] == (
        "inspect_runtime_or_requeue_task"
    )
    assert f"task:{fresh_provisioning.id}:stale_provisioning_task" not in items
    assert (
        f"task:{approval_provisioning.id}:stale_provisioning_task"
        not in items
    )
    assert f"task:{approval_provisioning.id}:approval_required" in items

    runner_item = items[f"task:{stale_runner_pin.id}:stale_runner_pin"]
    assert runner_item["label"] == "Runner pin stale"
    assert runner_item["runner_id"] == str(stale_runner.id)
    assert runner_item["lifecycle"]["mode"] == "runtime_lifecycle"
    assert runner_item["lifecycle"]["clears_when"] == (
        "runner_recovers_task_unpins_dispatches_terminal_or_archived"
    )
    assert runner_item["target"] == {
        "surface": "runtime",
        "project_id": str(project.id),
        "section": "devices",
        "task_id": str(stale_runner_pin.id),
        "runner_id": str(stale_runner.id),
    }
    assert f"task:{live_runner_pin.id}:stale_runner_pin" not in items
    assert f"task:{stale_runner_pin.id}:stale_provisioning_task" not in items

    actions = {entry["id"]: entry for entry in data["work_session"]["actions"]}
    queued_action = actions[f"attention.task:{stale_queued.id}:stale_queued_task"]
    assert queued_action["label"] == "Inspect queued dependency"
    assert queued_action["kind"] == "review"
    assert queued_action["target"] == {
        "surface": "task",
        "project_id": str(project.id),
        "task_id": str(stale_queued.id),
    }
    runner_action = actions[f"attention.task:{stale_runner_pin.id}:stale_runner_pin"]
    assert runner_action["label"] == "Check pinned runner"
    assert runner_action["target"] == {
        "surface": "runtime",
        "project_id": str(project.id),
        "section": "devices",
        "task_id": str(stale_runner_pin.id),
        "runner_id": str(stale_runner.id),
    }


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
    assert attention["has_more"] is False
    assert attention["limits"] == {
        "items": 8,
        "message_blocker_preview": 8,
        "message_question_preview": 8,
        "message_watch_preview": 24,
        "stale_task_preview": 4,
        "alert_error_preview": 4,
        "alert_warning_preview": 4,
        "stuck_acceptance_criteria_preview": 4,
    }
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
    assert all(item.get("lifecycle") for item in attention["items"])
    attention_items = {item["id"]: item for item in attention["items"]}
    question_lifecycle = attention_items[f"message:{question.id}"]["lifecycle"]
    assert question_lifecycle == {
        "mode": "message_lifecycle",
        "clears_when": "message_resolved_or_deleted",
        "operator_action": "reply_or_resolve_message",
        "acknowledgeable": True,
        "acknowledge_effect": "clears_unread_only",
    }
    alert_lifecycle = attention_items[f"alert:{active_alert.id}"]["lifecycle"]
    assert alert_lifecycle["mode"] == "alert_lifecycle"
    assert alert_lifecycle["clears_when"] == "alert_dismissed_or_resolved_at_source"
    assert alert_lifecycle["acknowledgeable"] is False
    assert attention_items[
        f"task:{approval_task.id}:approval_required"
    ]["movement_explanation"] == "Waiting for operator approval."
    approval_lifecycle = attention_items[
        f"task:{approval_task.id}:approval_required"
    ]["lifecycle"]
    assert approval_lifecycle["mode"] == "task_lifecycle"
    assert approval_lifecycle["operator_action"] == "approve_or_reject_task"
    failed_lifecycle = attention_items[f"task:{failed_task.id}:failed_task"][
        "lifecycle"
    ]
    assert failed_lifecycle["mode"] == "task_lifecycle"
    assert failed_lifecycle["clears_when"] == "task_requeued_archived_or_otherwise_resolved"
    assert failed_lifecycle["operator_action"] == "inspect_failed_task"

    actions = data["work_session"]["actions"]
    assert len(actions) <= 10
    actions_by_id = {entry["id"]: entry for entry in actions}
    question_action = actions_by_id[f"attention.message:{question.id}"]
    assert question_action["kind"] == "reply"
    assert question_action["label"] == "Reply to Strategic question"
    assert question_action["priority"] == "critical"
    assert question_action["target"] == {
        "surface": "messages",
        "project_id": str(project.id),
        "message_id": str(question.id),
        "action": "reply",
    }
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
    alert_action = actions_by_id[f"attention.alert:{active_alert.id}"]
    assert alert_action["target"] == {
        "surface": "runtime",
        "project_id": str(project.id),
        "section": "devices",
        "alert_id": str(active_alert.id),
    }
    assert all(entry["enabled"] is True for entry in actions)
    assert all(len(entry["label"]) <= 96 for entry in actions)
    assert all(len(entry["reason"]) <= 180 for entry in actions)

    commands = {entry["id"]: entry for entry in data["work_session"]["commands"]}
    mark_seen = commands["attention.messages.mark_seen"]
    assert mark_seen["mutation"]["method"] == "PUT"
    assert mark_seen["mutation"]["path"] == "/api/admin/messages/last-seen"
    assert mark_seen["mutation"]["body"]["project_id"] == str(project.id)
    assert isinstance(mark_seen["mutation"]["body"]["timestamp"], int)
    dismiss_alert = commands[f"attention.alert.dismiss:{active_alert.id}"]
    assert dismiss_alert["label"] == (
        f"Dismiss alert: {active_alert.title} ({str(active_alert.id)[:8]})"
    )
    assert dismiss_alert["mutation"] == {
        "method": "POST",
        "path": f"/api/admin/alerts/{active_alert.id}/dismiss",
        "body": {},
    }
    assert dismiss_alert["target"] == {
        "surface": "runtime",
        "project_id": str(project.id),
        "section": "devices",
        "alert_id": str(active_alert.id),
        "action": "dismiss_alert",
    }


@pytest.mark.asyncio
async def test_operator_attention_surfaces_stuck_acceptance_criteria(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    version = ReleaseVersion(project_id=project.id, version_number=1)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    c_no_map = await ac.create_criterion(
        db_session, project.id, version.id, "Unmapped criterion"
    )
    c_retired = await ac.create_criterion(
        db_session, project.id, version.id, "Retired criterion"
    )
    c_active = await ac.create_criterion(
        db_session, project.id, version.id, "Active criterion"
    )
    db_session.add(
        Direction(
            project_id=project.id,
            direction_key="d-retired",
            text="Retired mapping",
            status="retired",
            criterion_keys=[c_retired.criterion_key],
        )
    )
    db_session.add(
        Direction(
            project_id=project.id,
            direction_key="d-active",
            text="Active mapping",
            status="active",
            criterion_keys=[c_active.criterion_key],
        )
    )
    await db_session.commit()

    resp = await client.get(
        f"/api/admin/operator-attention?project_id={project.id}"
    )
    assert resp.status_code == 200
    attention = resp.json()

    assert attention["level"] == "needs_operator"
    assert attention["counts"]["stuck_acceptance_criteria"] == 2
    assert attention["counts"]["needs_operator"] == 3
    item_ids = {item["id"] for item in attention["items"]}
    no_map_id = f"version:{version.id}:acceptance_criterion:{c_no_map.criterion_key}"
    retired_id = f"version:{version.id}:acceptance_criterion:{c_retired.criterion_key}"
    active_id = f"version:{version.id}:acceptance_criterion:{c_active.criterion_key}"
    assert no_map_id in item_ids
    assert retired_id in item_ids
    assert active_id not in item_ids
    no_map_item = next(item for item in attention["items"] if item["id"] == no_map_id)
    retired_item = next(
        item for item in attention["items"] if item["id"] == retired_id
    )
    assert no_map_item["reason"] == "stuck_acceptance_criterion"
    assert no_map_item["stuck_reason"] == "no_mapping"
    assert no_map_item["label"] == "Acceptance criterion is unmapped"
    assert retired_item["stuck_reason"] == "direction_retired"
    assert (
        retired_item["label"]
        == "Acceptance criterion points to a retired direction"
    )


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
    assert attention["has_more"] is True
    item_ids = {item["id"] for item in attention["items"]}
    assert f"message:{old_question.id}" in item_ids
    assert f"task:{approval_task.id}:approval_required" in item_ids


@pytest.mark.asyncio
async def test_operator_attention_cap_prioritizes_non_message_operator_items(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    now = _utcnow()
    for idx in range(8):
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                message_type=MessageType.QUESTION,
                from_type="agent",
                content=f"Open question {idx}",
                status="open",
                created_at=now - timedelta(minutes=idx),
            )
        )
    approval_task = Task(
        project_id=project.id,
        title="Visible approval",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        requires_approval=True,
    )
    error_alert = Alert(
        project_id=project.id,
        severity=AlertSeverity.ERROR,
        title="Visible runtime error",
        message="Runner pool cannot dispatch work.",
        domain="runtime",
        event_type="dispatch_error",
    )
    db_session.add(approval_task)
    db_session.add(error_alert)
    await db_session.commit()

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention = resp.json()

    assert attention["counts"]["questions"] == 8
    assert attention["counts"]["approvals"] == 1
    assert attention["counts"]["error_alerts"] == 1
    assert attention["counts"]["needs_operator"] == 10
    assert len(attention["items"]) == 8
    assert attention["has_more"] is True
    item_ids = {item["id"] for item in attention["items"]}
    assert f"task:{approval_task.id}:approval_required" in item_ids
    assert f"alert:{error_alert.id}" in item_ids


@pytest.mark.asyncio
async def test_operator_attention_needs_preview_preserves_older_blockers(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    now = _utcnow()
    for idx in range(8):
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                message_type=MessageType.QUESTION,
                from_type="agent",
                content=f"Newer open question {idx}",
                status="open",
                created_at=now - timedelta(minutes=idx),
            )
        )
    older_blocker = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="agent",
        content="Older blocker must still stay visible.",
        status="open",
        created_at=now - timedelta(days=1),
    )
    db_session.add(older_blocker)
    await db_session.commit()

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention = resp.json()

    assert attention["counts"]["questions"] == 8
    assert attention["counts"]["blockers"] == 1
    assert len(attention["items"]) == 8
    assert attention["has_more"] is True
    assert f"message:{older_blocker.id}" in {
        item["id"] for item in attention["items"]
    }


@pytest.mark.asyncio
async def test_operator_attention_cap_prioritizes_local_runner_holds(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    now = _utcnow()
    for idx in range(8):
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                message_type=MessageType.QUESTION,
                from_type="agent",
                content=f"Open question before local runner hold {idx}",
                status="open",
                created_at=now - timedelta(minutes=idx),
            )
        )
    awaiting_since = (now - timedelta(minutes=30)).isoformat()
    local_hold = Task(
        project_id=project.id,
        title="Local hardware wait",
        payloads=[{"type": "compute_experiment"}],
        status=TaskStatus.QUEUED,
        latest_status_message="require_local_runner is waiting for hardware.",
        context={"awaiting_local_runner_since": awaiting_since},
        priority=1,
    )
    db_session.add(local_hold)
    await db_session.commit()

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention = resp.json()

    assert attention["counts"]["questions"] == 8
    assert attention["counts"]["local_runner_holds"] == 1
    assert len(attention["items"]) == 8
    assert attention["has_more"] is True
    assert f"task:{local_hold.id}:local_runner_hold" in {
        item["id"] for item in attention["items"]
    }


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
    items = {item["id"]: item for item in attention["items"]}
    bug_lifecycle = items[f"message:{bug.id}"]["lifecycle"]
    feedback_lifecycle = items[f"message:{feedback.id}"]["lifecycle"]
    assert bug_lifecycle["mode"] == "message_lifecycle"
    assert bug_lifecycle["acknowledge_effect"] == "clears_unread_only"
    assert feedback_lifecycle["mode"] == "message_lifecycle"
    assert feedback_lifecycle["acknowledgeable"] is True


@pytest.mark.asyncio
async def test_operator_attention_uses_routed_audience_for_action_messages(
    client: AsyncClient, db_session
):
    """Only the canonical action audience reaches needs_operator attention."""
    project = await _make_project(db_session)
    action = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="system",
        content="An operator decision is required.",
        status="open",
        context={"event_type": "criterion_retracted", "audience": "operator-action-needed"},
    )
    fyi = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="system",
        content="An operator-visible FYI.",
        status="open",
        context={"event_type": "capability_proof_gate_graduated_observe", "audience": "operator-fyi"},
    )
    internal = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="system",
        content="Strategist bookkeeping only.",
        status="open",
        context={"event_type": "pr_merged", "audience": "strategist-internal"},
    )
    db_session.add_all([action, fyi, internal])
    await db_session.commit()

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention = resp.json()

    assert attention["counts"]["needs_operator"] == 1
    items = {item["id"]: item for item in attention["items"]}
    assert items[f"message:{action.id}"]["severity"] == "needs_operator"
    assert f"message:{fyi.id}" not in items
    assert f"message:{internal.id}" not in items


@pytest.mark.asyncio
async def test_operator_attention_same_priority_items_use_newest_tie_break(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    now = _utcnow()
    messages = [
        ProjectMessage(
            project_id=project.id,
            message_type=MessageType.FEEDBACK,
            from_type="strategic",
            content=f"Feedback item {idx}",
            status="open",
            created_at=now - timedelta(minutes=idx),
        )
        for idx in range(10)
    ]
    db_session.add_all(messages)
    await db_session.commit()

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention = resp.json()

    assert attention["level"] == "watch"
    assert attention["counts"]["feedback"] == 10
    assert attention["has_more"] is True
    item_ids = [item["id"] for item in attention["items"]]
    expected_visible = [f"message:{message.id}" for message in messages[:8]]
    expected_hidden = {f"message:{message.id}" for message in messages[8:]}
    assert item_ids == expected_visible
    assert expected_hidden.isdisjoint(item_ids)


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
    items = {item["id"]: item for item in attention["items"]}
    manual_lifecycle = items["direction:d-active-manual:manual_intervention"][
        "lifecycle"
    ]
    assert manual_lifecycle["mode"] == "direction_lifecycle"
    assert manual_lifecycle["clears_when"] == "direction_flag_cleared_or_direction_inactive"
    assert manual_lifecycle["operator_action"] == "update_direction"


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
async def test_operator_attention_surfaces_preserved_handmerge_failed_task_as_needs_operator_and_clears_when_archived(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    preserved = Task(
        project_id=project.id,
        title="Preserved hand-merge PR",
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        requires_approval=True,
        latest_status_message="Review-fix cap reached (4 iterations, per-chain) - PR #123 LEFT OPEN with mergeable work; manual hand-merge required (not auto-closed)",
        context={
            "failure_detail": "review_cap_preserved_for_handmerge",
            "pr_preserved_for_handmerge": True,
        },
    )
    db_session.add(preserved)
    await db_session.commit()
    await db_session.refresh(preserved)

    newer_failed_tasks = []
    for idx in range(4):
        task = Task(
            project_id=project.id,
            title=f"New failure {idx + 1}",
            payloads=[{"type": "create"}],
            status=TaskStatus.FAILED,
            latest_status_message=f"Newer failure #{idx + 1}",
        )
        newer_failed_tasks.append(task)
    db_session.add_all(newer_failed_tasks)
    await db_session.commit()
    for task in newer_failed_tasks:
        await db_session.refresh(task)

    resp = await client.get(
        f"/api/admin/operator-attention?project_id={project.id}"
    )
    assert resp.status_code == 200
    attention = resp.json()

    assert attention["level"] == "needs_operator"
    assert attention["counts"]["needs_operator"] == 1
    assert attention["counts"]["watch"] == 4
    assert attention["counts"]["failed_tasks"] == 4
    assert attention["counts"]["handmerge_preserved_tasks"] == 1

    item_ids = {item["id"] for item in attention["items"]}
    preserved_id = f"task:{preserved.id}:hand_merge_required"
    assert preserved_id in item_ids
    assert f"task:{preserved.id}:failed_task" not in item_ids
    visible_failed_task_ids = {
        item["id"] for item in attention["items"] if item["reason"] == "failed_task"
    }
    assert len(visible_failed_task_ids) == 3
    assert visible_failed_task_ids.issubset(
        {f"task:{task.id}:failed_task" for task in newer_failed_tasks}
    )

    preserved_item = next(
        item for item in attention["items"] if item["id"] == preserved_id
    )
    assert preserved_item["severity"] == "needs_operator"
    assert preserved_item["reason"] == "hand_merge_required"
    assert preserved_item["label"] == "Hand-merge required"
    assert preserved_item["lifecycle"] == {
        "mode": "task_lifecycle",
        "clears_when": "task_archived_or_pr_merged_or_closed",
        "operator_action": "hand_merge_or_archive_task",
        "acknowledgeable": False,
        "acknowledge_effect": "none",
    }

    preserved.archived = True
    db_session.add(preserved)
    await db_session.commit()

    resp = await client.get(
        f"/api/admin/operator-attention?project_id={project.id}"
    )
    assert resp.status_code == 200
    attention = resp.json()

    assert attention["level"] == "watch"
    assert attention["counts"]["needs_operator"] == 0
    assert attention["counts"]["watch"] == 4
    assert attention["counts"]["failed_tasks"] == 4
    assert attention["counts"]["handmerge_preserved_tasks"] == 0
    assert preserved_id not in {item["id"] for item in attention["items"]}


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
    blocked_lifecycle = attention["items"][0]["lifecycle"]
    assert blocked_lifecycle["mode"] == "task_lifecycle"
    assert blocked_lifecycle["operator_action"] == "inspect_blocked_task"


@pytest.mark.asyncio
async def test_operator_attention_watch_lifecycle_for_parked_task_and_warning_alert(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    parked = Task(
        project_id=project.id,
        title="Parked without approval flag",
        payloads=[{"type": "create"}],
        status=TaskStatus.PARKED,
        requires_approval=False,
        park_reason="Waiting for a scheduled wake.",
    )
    warning = Alert(
        project_id=project.id,
        severity=AlertSeverity.WARNING,
        title="Runner warm pool warning",
        message="Warm pool is below target.",
        domain="runtime",
        event_type="warm_pool_warning",
    )
    db_session.add_all([parked, warning])
    await db_session.commit()

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention = resp.json()

    assert attention["level"] == "watch"
    assert attention["counts"]["needs_operator"] == 0
    assert attention["counts"]["watch"] == 2
    assert attention["counts"]["parked_tasks"] == 1
    assert attention["counts"]["warning_alerts"] == 1
    items = {item["id"]: item for item in attention["items"]}
    parked_item = items[f"task:{parked.id}:parked_task"]
    assert parked_item["severity"] == "watch"
    assert parked_item["lifecycle"]["clears_when"] == "task_unparked_terminal_or_archived"
    warning_item = items[f"alert:{warning.id}"]
    assert warning_item["severity"] == "watch"
    assert warning_item["lifecycle"]["mode"] == "alert_lifecycle"


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
    Pre-populates _CURRENCY_CACHE so the warm-cache path returns the stale verdict
    without awaiting GitHub (non-blocking design).
    """
    import time
    from app.routers.admin_project_strategic_session import _CURRENCY_CACHE_TTL_S

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
        "app.strategic_spawning.get_project_integration_branch",
        return_value="main",
    )
    cache_key = (str(project.id), "main", candidate_sha)
    currency = {"current": False, "candidate_sha": candidate_sha, "head_sha": head_sha}
    mocker.patch.dict(
        "app.routers.admin_project_strategic_session._CURRENCY_CACHE",
        {cache_key: (currency, time.monotonic() + _CURRENCY_CACHE_TTL_S)},
        clear=True,
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
    """Open version where candidate == integration HEAD → verdict fresh.

    Pre-populates _CURRENCY_CACHE so the warm-cache path returns the fresh verdict
    without awaiting GitHub (non-blocking design).
    """
    import time
    from app.routers.admin_project_strategic_session import _CURRENCY_CACHE_TTL_S

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
        "app.strategic_spawning.get_project_integration_branch",
        return_value="main",
    )
    cache_key = (str(project.id), "main", sha)
    currency = {"current": True, "candidate_sha": sha, "head_sha": sha}
    mocker.patch.dict(
        "app.routers.admin_project_strategic_session._CURRENCY_CACHE",
        {cache_key: (currency, time.monotonic() + _CURRENCY_CACHE_TTL_S)},
        clear=True,
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
    """On cold cache, sha_freshness.verdict is unknown (resolve runs off-path).
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
        "app.strategic_spawning.get_project_integration_branch",
        return_value="main",
    )
    # Cold cache → endpoint returns unknown immediately without awaiting GitHub.
    mocker.patch.dict(
        "app.routers.admin_project_strategic_session._CURRENCY_CACHE",
        {},
        clear=True,
    )

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    freshness = resp.json()["sha_freshness"]
    assert freshness["verdict"] == "unknown"


@pytest.mark.asyncio
async def test_strategic_state_sha_freshness_includes_checked_at(
    client: AsyncClient, db_session, mocker
):
    """Freshness block for a fresh verdict carries a checked_at ISO timestamp.

    Pre-populates _CURRENCY_CACHE so the warm-cache path returns the fresh
    verdict without awaiting GitHub (non-blocking design).
    """
    import time
    from app.routers.admin_project_strategic_session import _CURRENCY_CACHE_TTL_S

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
        "app.strategic_spawning.get_project_integration_branch",
        return_value="main",
    )
    cache_key = (str(project.id), "main", sha)
    currency = {"current": True, "candidate_sha": sha, "head_sha": sha}
    mocker.patch.dict(
        "app.routers.admin_project_strategic_session._CURRENCY_CACHE",
        {cache_key: (currency, time.monotonic() + _CURRENCY_CACHE_TTL_S)},
        clear=True,
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
    """Stale freshness block carries the integration_branch name used for comparison.

    Pre-populates _CURRENCY_CACHE so the warm-cache path returns the stale
    verdict without awaiting GitHub (non-blocking design).
    """
    import time
    from app.routers.admin_project_strategic_session import _CURRENCY_CACHE_TTL_S

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
        "app.strategic_spawning.get_project_integration_branch",
        return_value="automain",
    )
    cache_key = (str(project.id), "automain", candidate_sha)
    currency = {"current": False, "candidate_sha": candidate_sha, "head_sha": head_sha}
    mocker.patch.dict(
        "app.routers.admin_project_strategic_session._CURRENCY_CACHE",
        {cache_key: (currency, time.monotonic() + _CURRENCY_CACHE_TTL_S)},
        clear=True,
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


@pytest.mark.asyncio
async def test_strategic_state_sha_freshness_api_unreachable_distinct_from_unknown(
    client: AsyncClient, db_session, mocker
):
    """api_unreachable background results are NOT written to _CURRENCY_CACHE.

    With the non-blocking design, currency is resolved off the request path.
    A transient GitHub failure (api_unreachable) must not be cached — the next
    request should still trigger a fresh background resolution attempt rather
    than serving a pinned api_unreachable verdict for the full TTL.
    """
    from app.routers.admin_project_strategic_session import (
        _CURRENCY_CACHE,
        _currency_resolve_and_cache,
    )

    project = await _make_project(db_session)
    candidate = "abc1234" * 5
    version = ReleaseVersion(
        project_id=project.id,
        version_number=5,
        status="open",
        opened_at=_utcnow(),
        last_qualification={"candidate_sha": candidate, "status": "not_promoted"},
    )
    db_session.add(version)
    await db_session.commit()

    mocker.patch(
        "app.strategic_spawning._resolve_candidate_currency",
        new=AsyncMock(
            return_value={
                "verdict": "api_unreachable",
                "resolved": False,
                "reason": "github_http_502",
                "candidate_sha": candidate,
            }
        ),
    )
    mocker.patch(
        "app.strategic_spawning.get_project_integration_branch",
        return_value="main",
    )
    mocker.patch.dict(
        "app.routers.admin_project_strategic_session._CURRENCY_CACHE",
        {},
        clear=True,
    )

    # Cold cache → endpoint returns unknown immediately (GitHub not awaited).
    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    freshness = resp.json()["sha_freshness"]
    assert freshness["verdict"] == "unknown"

    # Run the background coroutine directly to verify api_unreachable is not cached.
    cache_key = (str(project.id), "main", candidate)
    last_qual = {"candidate_sha": candidate, "status": "not_promoted"}
    await _currency_resolve_and_cache(project.id, last_qual, "main", cache_key)

    # api_unreachable must NOT be written to the cache.
    assert cache_key not in _CURRENCY_CACHE, (
        "api_unreachable verdict must not be cached; found in _CURRENCY_CACHE"
    )


@pytest.mark.asyncio
async def test_strategic_state_currency_is_non_blocking(
    client: AsyncClient, db_session, mocker
):
    """GET /strategic-state must return promptly on a cold currency cache.

    Acceptance test: with _resolve_candidate_currency patched to sleep 2 s the
    endpoint must return 'unknown' (or a previously-cached verdict) immediately
    instead of blocking.  After the background task runs, _CURRENCY_CACHE is
    populated for subsequent callers.
    """
    import asyncio
    import time
    from app.routers.admin_project_strategic_session import _CURRENCY_CACHE

    project = await _make_project(db_session)
    sha = "deadbeef" * 5
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        opened_at=_utcnow(),
        last_qualification={"candidate_sha": sha, "status": "not_promoted"},
    )
    db_session.add(version)
    await db_session.commit()

    mocker.patch(
        "app.strategic_spawning.get_project_integration_branch",
        return_value="main",
    )

    resolved_event = asyncio.Event()

    async def _slow_resolve(*args, **kwargs):
        await asyncio.sleep(2)
        resolved_event.set()
        return {"current": True, "candidate_sha": sha, "head_sha": sha}

    mocker.patch(
        "app.strategic_spawning._resolve_candidate_currency",
        new=_slow_resolve,
    )
    # Ensure cold cache for this project/branch/sha combination.
    mocker.patch.dict(
        "app.routers.admin_project_strategic_session._CURRENCY_CACHE",
        {},
        clear=True,
    )

    # Capture background tasks so we can await them after the response.
    bg_tasks: list[asyncio.Task] = []
    original_create_task = asyncio.create_task

    def _capture_create_task(coro, **kw):
        t = original_create_task(coro, **kw)
        bg_tasks.append(t)
        return t

    mocker.patch("asyncio.create_task", side_effect=_capture_create_task)

    # --- BEFORE measurement ---
    t0 = time.monotonic()
    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    elapsed = time.monotonic() - t0

    assert resp.status_code == 200
    freshness = resp.json()["sha_freshness"]
    # Cold cache → endpoint returns unknown without waiting on GitHub.
    assert freshness["verdict"] == "unknown", (
        f"Expected 'unknown' on cold cache, got {freshness['verdict']!r}"
    )
    # Must return well under the 2 s mock sleep — proves it is non-blocking.
    assert elapsed < 1.0, (
        f"Endpoint took {elapsed:.2f}s; expected non-blocking return under 1s"
    )
    # A background task must have been scheduled.
    assert bg_tasks, "asyncio.create_task was not called — background resolution not scheduled"

    # --- AFTER: wait for background task, then verify cache is populated ---
    await asyncio.gather(*bg_tasks, return_exceptions=True)
    cache_key = (str(project.id), "main", sha)
    assert cache_key in _CURRENCY_CACHE, (
        "Background resolution did not populate _CURRENCY_CACHE"
    )
    cached_currency, _ = _CURRENCY_CACHE[cache_key]
    assert cached_currency.get("current") is True


@pytest.mark.asyncio
async def test_strategic_runtime_compute_job_preview_includes_experiment_outcome(
    client: AsyncClient, db_session
):
    """_compute_job_preview populates experiment_outcome from task.plan_output.compute_result."""
    project = await _make_project(db_session)
    now = _utcnow()
    task = Task(
        project_id=project.id,
        title="Throughput regression check",
        payloads=[{"type": "compute_experiment"}],
        status=TaskStatus.COMPLETED,
        plan_output={
            "compute_result": {
                "criterion": {
                    "met": True,
                    "measurable": True,
                    "summary": "Throughput exceeded 100 req/s",
                },
                "hypothesis": {"held": True},
            }
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend="local",
        machine_type="gpu-small",
        status=ComputeJobStatus.SUCCEEDED,
        final_cost_usd=3.5,
        completed_at=now - timedelta(minutes=15),
        updated_at=now - timedelta(minutes=15),
    )
    db_session.add(job)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    data = resp.json()
    items = data["runtime"]["work"]["compute_jobs"]["items"]
    matched = [item for item in items if item.get("task_id") == str(task.id)]
    assert matched, "compute job preview item not found for task"
    outcome = matched[0].get("experiment_outcome")
    assert outcome is not None, "experiment_outcome missing from compute job preview item"
    assert outcome["outcome_label"] == "met"
    assert outcome["criterion_met"] is True
    assert outcome["hypothesis_held"] is True
    assert "Throughput exceeded 100 req/s" in (outcome.get("criterion_summary") or "")


@pytest.mark.asyncio
async def test_strategic_runtime_compute_job_preview_inconclusive_when_not_measurable(
    client: AsyncClient, db_session
):
    """met=false, measurable=false → outcome_label inconclusive, not unmet."""
    project = await _make_project(db_session)
    now = _utcnow()
    task = Task(
        project_id=project.id,
        title="Non-measurable metric check",
        payloads=[{"type": "compute_experiment"}],
        status=TaskStatus.COMPLETED,
        plan_output={
            "compute_result": {
                "criterion": {
                    "met": False,
                    "measurable": False,
                    "summary": "Metric not found in output",
                },
            }
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend="local",
        status=ComputeJobStatus.SUCCEEDED,
        final_cost_usd=1.0,
        completed_at=now - timedelta(minutes=5),
        updated_at=now - timedelta(minutes=5),
    )
    db_session.add(job)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    data = resp.json()
    items = data["runtime"]["work"]["compute_jobs"]["items"]
    matched = [item for item in items if item.get("task_id") == str(task.id)]
    assert matched, "compute job preview item not found"
    outcome = matched[0].get("experiment_outcome")
    assert outcome is not None
    assert outcome["outcome_label"] == "inconclusive"
    assert outcome["criterion_met"] is False


@pytest.mark.asyncio
async def test_operator_attention_surfaces_unmet_experiment_outcome(
    client: AsyncClient, db_session
):
    """A recently-completed compute_experiment with criterion.met=False appears in attention."""
    project = await _make_project(db_session)
    now = _utcnow()
    unmet_task = Task(
        project_id=project.id,
        title="Latency regression check",
        payloads=[{"type": "compute_experiment"}],
        status=TaskStatus.COMPLETED,
        archived=False,
        plan_output={
            "compute_result": {
                "criterion": {
                    "met": False,
                    "measurable": True,
                    "summary": "P99 latency was 380ms, threshold 200ms",
                }
            }
        },
    )
    # met=True → should NOT appear
    met_task = Task(
        project_id=project.id,
        title="Throughput benchmark met",
        payloads=[{"type": "compute_experiment"}],
        status=TaskStatus.COMPLETED,
        archived=False,
        completed_at=now - timedelta(hours=2),
        plan_output={
            "compute_result": {
                "criterion": {"met": True, "measurable": True, "summary": "All good"}
            }
        },
    )
    # older than 6 hours → should NOT appear
    old_task = Task(
        project_id=project.id,
        title="Old benchmark",
        payloads=[{"type": "compute_experiment"}],
        status=TaskStatus.COMPLETED,
        archived=False,
        completed_at=now - timedelta(hours=8),
        plan_output={
            "compute_result": {
                "criterion": {"met": False, "measurable": True, "summary": "Old failure"}
            }
        },
    )
    # archived=True → should NOT appear
    archived_task = Task(
        project_id=project.id,
        title="Archived benchmark",
        payloads=[{"type": "compute_experiment"}],
        status=TaskStatus.COMPLETED,
        archived=True,
        completed_at=now - timedelta(hours=1),
        plan_output={
            "compute_result": {
                "criterion": {"met": False, "measurable": True, "summary": "Archived"}
            }
        },
    )
    db_session.add_all([unmet_task, met_task, old_task, archived_task])
    await db_session.commit()
    await db_session.refresh(unmet_task)
    assert unmet_task.completed_at is not None

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention = resp.json()

    assert attention["counts"]["experiment_outcomes"] == 1
    assert attention["counts"]["watch"] >= 1
    item_ids = {item["id"] for item in attention["items"]}
    expected_id = f"task:{unmet_task.id}:experiment_outcome"
    assert expected_id in item_ids
    exp_item = next(item for item in attention["items"] if item["id"] == expected_id)
    assert exp_item["reason"] == "experiment_outcome"
    assert exp_item["severity"] == "watch"
    assert "unmet" in exp_item["label"].lower()
    assert exp_item["lifecycle"]["mode"] == "task_lifecycle"
    assert "P99 latency" in (exp_item.get("detail") or "")

    # met / old / archived tasks must not appear
    assert f"task:{met_task.id}:experiment_outcome" not in item_ids
    assert f"task:{old_task.id}:experiment_outcome" not in item_ids
    assert f"task:{archived_task.id}:experiment_outcome" not in item_ids


@pytest.mark.asyncio
async def test_operator_attention_inconclusive_experiment_outcome_label(
    client: AsyncClient, db_session
):
    """met=False, measurable=False → outcome_label inconclusive, not unmet."""
    project = await _make_project(db_session)
    now = _utcnow()
    inconclusive_task = Task(
        project_id=project.id,
        title="Non-measurable criterion experiment",
        payloads=[{"type": "compute_experiment"}],
        status=TaskStatus.COMPLETED,
        archived=False,
        completed_at=now - timedelta(hours=1),
        plan_output={
            "compute_result": {
                "criterion": {
                    "met": False,
                    "measurable": False,
                    "summary": "Metric absent from run output",
                }
            }
        },
    )
    db_session.add(inconclusive_task)
    await db_session.commit()

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention = resp.json()

    assert attention["counts"]["experiment_outcomes"] == 1
    item_ids = {item["id"] for item in attention["items"]}
    expected_id = f"task:{inconclusive_task.id}:experiment_outcome"
    assert expected_id in item_ids
    exp_item = next(item for item in attention["items"] if item["id"] == expected_id)
    assert "inconclusive" in exp_item["label"].lower()


@pytest.mark.asyncio
async def test_operator_attention_unmet_experiment_not_masked_by_newer_successes(
    client: AsyncClient, db_session
):
    """An unmet outcome must be counted even when many successful experiments
    complete after it within the window (no fixed-size scan cap)."""
    project = await _make_project(db_session)
    now = _utcnow()
    unmet_task = Task(
        project_id=project.id,
        title="Buried unmet experiment",
        payloads=[{"type": "compute_experiment"}],
        status=TaskStatus.COMPLETED,
        archived=False,
        completed_at=now - timedelta(hours=5),
        plan_output={
            "compute_result": {
                "criterion": {
                    "met": False,
                    "measurable": True,
                    "summary": "Throughput below threshold",
                }
            }
        },
    )
    met_tasks = [
        Task(
            project_id=project.id,
            title=f"Successful experiment {i}",
            payloads=[{"type": "compute_experiment"}],
            status=TaskStatus.COMPLETED,
            archived=False,
            completed_at=now - timedelta(minutes=i),
            plan_output={
                "compute_result": {
                    "criterion": {"met": True, "measurable": True, "summary": "ok"}
                }
            },
        )
        for i in range(25)
    ]
    db_session.add_all([unmet_task, *met_tasks])
    await db_session.commit()

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention = resp.json()

    assert attention["counts"]["experiment_outcomes"] == 1
    item_ids = {item["id"] for item in attention["items"]}
    assert f"task:{unmet_task.id}:experiment_outcome" in item_ids


@pytest.mark.asyncio
async def test_operator_attention_surfaces_stuck_criteria_no_mapping(
    client: AsyncClient, db_session
):
    """Uncovered criterion with no direction mapping surfaces as needs_operator stuck_criteria."""
    project = await _make_project(db_session)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        opened_at=_utcnow(),
    )
    db_session.add(version)
    await db_session.flush()

    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key="c-aabbccdd",
        text="All tests pass",
        status="active",
    )
    db_session.add(criterion)
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    attention = resp.json()["attention"]

    assert attention["counts"]["stuck_criteria"] >= 1
    assert attention["counts"]["needs_operator"] >= 1
    assert attention["level"] == "needs_operator"

    stuck_items = [item for item in attention["items"] if item["reason"] == "stuck_criteria"]
    assert len(stuck_items) == 1
    item = stuck_items[0]
    assert item["severity"] == "needs_operator"
    assert "no direction mapping" in item["detail"]
    assert item["lifecycle"]["operator_action"] == "remap_or_attest_criterion"


@pytest.mark.asyncio
async def test_operator_attention_surfaces_stuck_criteria_direction_retired(
    client: AsyncClient, db_session
):
    """Uncovered criterion whose only mapped direction is retired surfaces as stuck_criteria."""
    project = await _make_project(db_session)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        opened_at=_utcnow(),
    )
    db_session.add(version)
    await db_session.flush()

    criterion_key = "c-11223344"
    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=criterion_key,
        text="Performance within budget",
        status="active",
    )
    retired_direction = Direction(
        project_id=project.id,
        direction_key="d-retired-dir",
        status="retired",
        criterion_keys=[criterion_key],
    )
    db_session.add_all([criterion, retired_direction])
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    attention = resp.json()["attention"]

    assert attention["counts"]["stuck_criteria"] >= 1
    assert attention["counts"]["needs_operator"] >= 1
    assert attention["level"] == "needs_operator"

    stuck_items = [item for item in attention["items"] if item["reason"] == "stuck_criteria"]
    assert len(stuck_items) == 1
    item = stuck_items[0]
    assert item["severity"] == "needs_operator"
    assert "retired" in item["detail"]
    assert item["lifecycle"]["operator_action"] == "remap_or_attest_criterion"


# ── sweep_closed_handmerge_prs tests ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_sweep_closed_handmerge_prs_resolves_merged_pr(db_session, mocker):
    """When a preserved-for-handmerge task's PR has been merged, the sweep clears
    pr_preserved_for_handmerge from context and resolves the ProjectMessage."""
    from app.reaper_sweeps import sweep_closed_handmerge_prs

    project = await _make_project(db_session)
    task = Task(
        project_id=project.id,
        title="Rollup for hand-merge",
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        requires_approval=True,
        context={
            "pr_preserved_for_handmerge": True,
            "handmerge_pr_number": 999,
            "failure_detail": "review_cap_preserved_for_handmerge",
        },
    )
    db_session.add(task)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="system",
        from_task_id=None,
        content="PR #999 preserved for hand-merge",
        context={"event_type": "handmerge_pr_preserved", "pr_number": 999},
        idempotency_key=None,  # set after task.id is known
    )
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(task)

    # Now fix the idempotency_key (task.id available after commit).
    msg.idempotency_key = f"handmerge:{task.id}"
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"state": "closed", "merged_at": "2026-07-05T10:00:00Z"},
    )

    resolved = await sweep_closed_handmerge_prs(db_session)

    assert resolved == 1
    await db_session.refresh(task)
    ctx = task.context or {}
    assert "pr_preserved_for_handmerge" not in ctx, "Flag must be cleared after merge"
    assert ctx.get("handmerge_pr_resolved") is True
    assert ctx.get("handmerge_pr_resolution") == "merged"

    await db_session.refresh(msg)
    assert msg.status == "resolved"
    assert msg.resolution == "PR #999 merged"
    assert msg.resolved_at is not None


@pytest.mark.asyncio
async def test_sweep_closed_handmerge_prs_resolves_closed_unmerged_pr(db_session, mocker):
    """When a preserved PR is closed without merging, the sweep clears the flag
    and resolves the message with resolution='closed'."""
    from app.reaper_sweeps import sweep_closed_handmerge_prs

    project = await _make_project(db_session)
    task = Task(
        project_id=project.id,
        title="Rollup closed without merge",
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        requires_approval=True,
        context={
            "pr_preserved_for_handmerge": True,
            "handmerge_pr_number": 888,
            "failure_detail": "review_cap_preserved_for_handmerge",
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="system",
        content="PR #888 preserved for hand-merge",
        context={"event_type": "handmerge_pr_preserved", "pr_number": 888},
        idempotency_key=f"handmerge:{task.id}",
    )
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"state": "closed", "merged_at": None},
    )

    resolved = await sweep_closed_handmerge_prs(db_session)

    assert resolved == 1
    await db_session.refresh(task)
    assert "pr_preserved_for_handmerge" not in (task.context or {})
    assert (task.context or {}).get("handmerge_pr_resolution") == "closed"

    await db_session.refresh(msg)
    assert msg.status == "resolved"
    assert msg.resolution == "PR #888 closed"


@pytest.mark.asyncio
async def test_sweep_closed_handmerge_prs_skips_open_pr(db_session, mocker):
    """When the PR is still open, the sweep must not clear the flag or resolve
    the message."""
    from app.reaper_sweeps import sweep_closed_handmerge_prs

    project = await _make_project(db_session)
    task = Task(
        project_id=project.id,
        title="Rollup still open",
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        requires_approval=True,
        context={
            "pr_preserved_for_handmerge": True,
            "handmerge_pr_number": 777,
            "failure_detail": "review_cap_preserved_for_handmerge",
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="system",
        content="PR #777 preserved for hand-merge",
        context={"event_type": "handmerge_pr_preserved", "pr_number": 777},
        idempotency_key=f"handmerge:{task.id}",
    )
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"state": "open", "merged_at": None},
    )

    resolved = await sweep_closed_handmerge_prs(db_session)

    assert resolved == 0
    await db_session.refresh(task)
    assert (task.context or {}).get("pr_preserved_for_handmerge") is True, (
        "Flag must NOT be cleared while PR is still open"
    )
    await db_session.refresh(msg)
    assert msg.status == "open", "Message must remain open while PR is open"
    # Fairness: last-checked timestamp must be stamped so updated_at advances,
    # rotating this task to the back of the ordered batch in future cycles.
    assert (task.context or {}).get("handmerge_last_checked_at") is not None, (
        "handmerge_last_checked_at must be set after an open-PR check"
    )


@pytest.mark.asyncio
async def test_sweep_closed_handmerge_prs_legacy_fallback_artifact(db_session, mocker):
    """When a preserved task has no handmerge_pr_number in context (legacy path),
    the sweep falls back to the PULL_REQUEST artifact's pr_url to find the PR number,
    and still clears the flag and resolves the ProjectMessage when the PR is closed."""
    from app.reaper_sweeps import sweep_closed_handmerge_prs

    project = await _make_project(db_session)
    # Task does NOT have handmerge_pr_number in context — simulates tasks
    # preserved before handmerge_pr_number was stored in context.
    task = Task(
        project_id=project.id,
        title="Legacy hand-merge no PR number in context",
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        requires_approval=True,
        context={
            "pr_preserved_for_handmerge": True,
            "failure_detail": "review_cap_preserved_for_handmerge",
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # PULL_REQUEST artifact provides the PR URL (legacy path).
    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/owner/repo/pull/404",
    )
    db_session.add(artifact)

    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.NOTE,
        from_type="system",
        content="PR preserved for hand-merge",
        context={"event_type": "handmerge_pr_preserved", "pr_number": 404},
        idempotency_key=f"handmerge:{task.id}",
    )
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"state": "closed", "merged_at": "2026-07-05T11:00:00Z"},
    )

    resolved = await sweep_closed_handmerge_prs(db_session)

    assert resolved == 1
    await db_session.refresh(task)
    ctx = task.context or {}
    assert "pr_preserved_for_handmerge" not in ctx, (
        "Flag must be cleared even when PR number came from artifact fallback"
    )
    assert ctx.get("handmerge_pr_resolved") is True
    assert ctx.get("handmerge_pr_resolution") == "merged"

    await db_session.refresh(msg)
    assert msg.status == "resolved"
    assert msg.resolution == "PR #404 merged"
    assert msg.resolved_at is not None


@pytest.mark.asyncio
async def test_operator_attention_clears_after_sweep_resolves_handmerge_pr(
    client: AsyncClient, db_session, mocker
):
    """End-to-end: after sweep_closed_handmerge_prs clears pr_preserved_for_handmerge,
    the operator_attention endpoint no longer shows a hand_merge_required item."""
    from app.reaper_sweeps import sweep_closed_handmerge_prs

    project = await _make_project(db_session)
    task = Task(
        project_id=project.id,
        title="Preserved hand-merge PR — sweep integration",
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        requires_approval=True,
        context={
            "pr_preserved_for_handmerge": True,
            "handmerge_pr_number": 555,
            "failure_detail": "review_cap_preserved_for_handmerge",
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Before sweep: attention shows the hand_merge_required item.
    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention_before = resp.json()
    assert attention_before["counts"]["handmerge_preserved_tasks"] == 1
    assert attention_before["counts"]["needs_operator"] == 1
    assert any(
        item["reason"] == "hand_merge_required"
        for item in attention_before["items"]
    )

    # Simulate PR being merged by having GitHub return state=closed.
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"state": "closed", "merged_at": "2026-07-05T10:00:00Z"},
    )

    resolved = await sweep_closed_handmerge_prs(db_session)
    assert resolved == 1

    # After sweep: attention no longer shows the hand_merge_required item.
    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention_after = resp.json()
    assert attention_after["counts"]["handmerge_preserved_tasks"] == 0
    assert attention_after["counts"]["needs_operator"] == 0
    assert not any(
        item["reason"] == "hand_merge_required"
        for item in attention_after["items"]
    )


@pytest.mark.asyncio
async def test_coverage_blocked_version_without_stuck_criteria_raises_attention(
    client: AsyncClient, db_session
):
    """Uncovered criterion mapped to an active direction raises coverage_blocked attention."""
    project = await _make_project(db_session)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        opened_at=_utcnow(),
    )
    db_session.add(version)
    await db_session.flush()

    criterion_key = "c-cov-blocked-01"
    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key=criterion_key,
        text="Feature X fully shipped",
        status="active",
    )
    # Map the criterion to an active direction (stuck_reason will be None).
    active_direction = Direction(
        project_id=project.id,
        direction_key="d-active-for-cov",
        status="active",
        criterion_keys=[criterion_key],
    )
    db_session.add_all([criterion, active_direction])
    await db_session.commit()

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    attention = resp.json()["attention"]

    # coverage_blocked should be counted and surfaced as needs_operator
    assert attention["counts"]["coverage_blocked"] >= 1
    assert attention["counts"]["needs_operator"] >= 1
    assert attention["level"] == "needs_operator"

    # stuck_criteria must NOT fire for this criterion (it has an active mapping)
    assert attention["counts"]["stuck_criteria"] == 0

    coverage_blocked_items = [
        item for item in attention["items"] if item["reason"] == "coverage_blocked"
    ]
    assert len(coverage_blocked_items) == 1
    cb_item = coverage_blocked_items[0]
    assert cb_item["severity"] == "needs_operator"
    assert "active" in cb_item["detail"]
    assert cb_item["lifecycle"]["operator_action"] == "monitor_active_direction_progress"


@pytest.mark.asyncio
async def test_operator_attention_surfaces_stale_completion_debt(
    client: AsyncClient, db_session
):
    """Open debt older than the threshold with no linked tasks → needs_operator item."""
    project = await _make_project(db_session)
    old_entry = CompletionDebtEntry(
        project_id=project.id,
        kind="dormant_knob",
        title="Old unworked knob obligation",
        status="open",
        created_at=_utcnow() - timedelta(hours=48),
    )
    db_session.add(old_entry)
    await db_session.commit()

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention = resp.json()

    assert attention["counts"]["stale_completion_debts"] == 1
    assert attention["counts"]["needs_operator"] >= 1
    assert attention["level"] == "needs_operator"
    stale_items = [i for i in attention["items"] if i["reason"] == "stale_completion_debt"]
    assert len(stale_items) == 1
    item = stale_items[0]
    assert item["severity"] == "needs_operator"
    assert item["count"] == 1
    assert item["lifecycle"]["operator_action"] == "link_task_or_resolve_debt"


@pytest.mark.asyncio
async def test_operator_attention_does_not_surface_young_completion_debt(
    client: AsyncClient, db_session
):
    """Open debt within the staleness threshold is NOT surfaced."""
    project = await _make_project(db_session)
    young_entry = CompletionDebtEntry(
        project_id=project.id,
        kind="dormant_knob",
        title="Recently created knob obligation",
        status="open",
        created_at=_utcnow() - timedelta(hours=1),
    )
    db_session.add(young_entry)
    await db_session.commit()

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention = resp.json()

    assert attention["counts"]["stale_completion_debts"] == 0
    stale_items = [i for i in attention["items"] if i["reason"] == "stale_completion_debt"]
    assert len(stale_items) == 0


@pytest.mark.asyncio
async def test_operator_attention_does_not_surface_debt_with_linked_tasks(
    client: AsyncClient, db_session
):
    """Old debt linked to a genuinely active Task is NOT surfaced (work is in progress)."""
    project = await _make_project(db_session)
    # Create a real Task in an active status so the linked-task resolver finds it.
    active_task = Task(
        project_id=project.id,
        title="In-flight work addressing the debt",
        payloads=[{"type": "fix"}],
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(active_task)
    await db_session.flush()
    linked_entry = CompletionDebtEntry(
        project_id=project.id,
        kind="dormant_knob",
        title="Old knob obligation with work in flight",
        status="open",
        created_at=_utcnow() - timedelta(hours=48),
        detail={"linked_tasks": [{"task_id": str(active_task.id)}]},
    )
    db_session.add(linked_entry)
    await db_session.commit()

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention = resp.json()

    assert attention["counts"]["stale_completion_debts"] == 0
    stale_items = [i for i in attention["items"] if i["reason"] == "stale_completion_debt"]
    assert len(stale_items) == 0


@pytest.mark.asyncio
async def test_operator_attention_surfaces_debt_with_only_failed_linked_task(
    client: AsyncClient, db_session
):
    """Old debt whose only linked task is FAILED resurfaces as stale-unworked.

    A FAILED linked task does NOT count as work-in-progress; the debt should
    surface so the operator/strategist can spawn a new follow-up or dispose it.
    """
    project = await _make_project(db_session)
    failed_task = Task(
        project_id=project.id,
        title="Failed follow-up for the debt",
        payloads=[{"type": "fix"}],
        status=TaskStatus.FAILED,
    )
    db_session.add(failed_task)
    await db_session.flush()
    linked_entry = CompletionDebtEntry(
        project_id=project.id,
        kind="dormant_knob",
        title="Knob obligation whose only follow-up failed",
        status="open",
        created_at=_utcnow() - timedelta(hours=48),
        detail={"linked_tasks": [{"task_id": str(failed_task.id)}]},
    )
    db_session.add(linked_entry)
    await db_session.commit()

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention = resp.json()

    # The debt should resurface because the only linked task failed.
    assert attention["counts"]["stale_completion_debts"] >= 1
    stale_items = [i for i in attention["items"] if i["reason"] == "stale_completion_debt"]
    assert len(stale_items) >= 1


@pytest.mark.asyncio
async def test_operator_attention_does_not_surface_resolved_or_dismissed_debt(
    client: AsyncClient, db_session
):
    """Resolved or dismissed debts are not surfaced regardless of age."""
    project = await _make_project(db_session)
    resolved_entry = CompletionDebtEntry(
        project_id=project.id,
        kind="dormant_knob",
        title="Resolved old knob",
        status="resolved",
        created_at=_utcnow() - timedelta(hours=48),
    )
    dismissed_entry = CompletionDebtEntry(
        project_id=project.id,
        kind="dormant_knob",
        title="Dismissed old knob",
        status="dismissed",
        created_at=_utcnow() - timedelta(hours=48),
    )
    db_session.add(resolved_entry)
    db_session.add(dismissed_entry)
    await db_session.commit()

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention = resp.json()

    assert attention["counts"]["stale_completion_debts"] == 0
    stale_items = [i for i in attention["items"] if i["reason"] == "stale_completion_debt"]
    assert len(stale_items) == 0


@pytest.mark.asyncio
async def test_operator_attention_stale_debt_count_in_counts_block(
    client: AsyncClient, db_session
):
    """counts.stale_completion_debts reflects both project-scoped and global (project_id=NULL) rows."""
    project = await _make_project(db_session)
    project_entry = CompletionDebtEntry(
        project_id=project.id,
        kind="dormant_knob",
        title="Project-scoped old debt",
        status="open",
        created_at=_utcnow() - timedelta(hours=48),
    )
    global_entry = CompletionDebtEntry(
        project_id=None,
        kind="seed_inventory",
        title="Global deployment-level old debt",
        status="open",
        created_at=_utcnow() - timedelta(hours=72),
    )
    db_session.add(project_entry)
    db_session.add(global_entry)
    await db_session.commit()

    resp = await client.get(f"/api/admin/operator-attention?project_id={project.id}")
    assert resp.status_code == 200
    attention = resp.json()

    assert attention["counts"]["stale_completion_debts"] == 2
    stale_items = [i for i in attention["items"] if i["reason"] == "stale_completion_debt"]
    assert len(stale_items) == 1
    assert stale_items[0]["count"] == 2


@pytest.mark.asyncio
async def test_strategic_state_includes_state_revision(
    client: AsyncClient, db_session
):
    """state_revision appears in every response as a non-empty hex string."""
    project = await _make_project(db_session)

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    data = resp.json()

    revision = data.get("state_revision")
    assert revision is not None, "state_revision must be present in response"
    assert isinstance(revision, str), "state_revision must be a string"
    assert len(revision) == 16, "state_revision must be a 16-char hex digest"
    assert all(c in "0123456789abcdef" for c in revision), (
        "state_revision must be a hex string"
    )


@pytest.mark.asyncio
async def test_strategic_state_matching_revision_returns_304_skips_assembly(
    client: AsyncClient, db_session, mocker
):
    """ACCEPTANCE: a matching broad state_revision returns HTTP 304 with an EMPTY
    body BEFORE any Phase-2 assembly runs — no context-package build, no runtime
    summary, no workbench state, no event feed. This is the server-side
    assembly-skip (distinct from the post-assembly ETag 304 which only saves
    transmission)."""
    project = await _make_project(db_session)

    # First call to get the current broad revision token.
    resp1 = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp1.status_code == 200
    revision = resp1.json().get("state_revision")
    assert revision is not None

    # Spy on representative Phase-1 (context) AND Phase-2 (assembly) builders to
    # prove NONE of them run on a matching revision.
    evidence_spy = mocker.patch(
        "app.routers.admin_project_strategic_session.load_strategic_context_evidence",
        new_callable=AsyncMock,
    )
    build_spy = mocker.patch(
        "app.routers.admin_project_strategic_session.build_strategic_context_package_summary",
    )
    runtime_spy = mocker.patch(
        "app.routers.admin_project_strategic_session.build_strategic_runtime_summary",
        new_callable=AsyncMock,
    )
    workbench_spy = mocker.patch(
        "app.routers.admin_project_strategic_session.get_strategic_workbench_state",
        new_callable=AsyncMock,
    )
    events_spy = mocker.patch(
        "app.routers.admin_project_strategic_session.build_workbench_event_feed",
        new_callable=AsyncMock,
    )
    attention_spy = mocker.patch(
        "app.routers.admin_project_strategic_session.build_operator_attention",
        new_callable=AsyncMock,
    )

    # Second call WITH the matching revision token → 304, empty body.
    resp2 = await client.get(
        f"/api/admin/projects/{project.id}/strategic-state?state_revision={revision}"
    )
    assert resp2.status_code == 304, (
        f"matching revision must 304; got {resp2.status_code} "
        f"body={resp2.text[:200]!r}"
    )
    assert len(resp2.content) == 0, "304 assembly-skip must have an empty body"

    # NONE of the expensive builders ran — the assembly was skipped, not just the
    # transmission.
    evidence_spy.assert_not_called()
    build_spy.assert_not_called()
    runtime_spy.assert_not_called()
    workbench_spy.assert_not_called()
    events_spy.assert_not_called()
    attention_spy.assert_not_called()


# The visible response segments and the mutation that must flip the broad
# revision for each. Guards against a false 304 hiding a real change.
@pytest.mark.asyncio
async def test_strategic_state_revision_changes_when_each_segment_changes(
    client: AsyncClient, db_session
):
    """The broad revision must CHANGE when any fingerprinted segment changes, so a
    held token never suppresses a real update. Exercises workbench, runners,
    compute jobs, wakes, messages, and the open release version end-to-end."""
    project = await _make_project(db_session)

    async def _current_revision() -> str:
        resp = await client.get(
            f"/api/admin/projects/{project.id}/strategic-state"
        )
        assert resp.status_code == 200
        rev = resp.json().get("state_revision")
        assert rev is not None
        return rev

    async def _assert_flips(label: str, mutate) -> None:
        before = await _current_revision()
        await mutate()
        await db_session.commit()
        after = await _current_revision()
        assert after != before, (
            f"{label}: broad revision must change so no 304 hides the update "
            f"(before={before} after={after})"
        )

    now = _utcnow()

    # 1. Workbench (MessagesNoisePrefs) updated.
    async def _mutate_workbench():
        db_session.add(
            MessagesNoisePrefs(
                user_id=TEST_USER_ID,
                project_id=project.id,
                prefs={
                    "_strategic_workbench": {
                        "version": 1,
                        "interaction_mode": "continuous",
                        "active_surface": "runtime",
                        "selected_refs": [],
                    },
                },
            )
        )

    await _assert_flips("workbench", _mutate_workbench)

    # 2. A new enrolled runner (count + max last_seen fingerprint).
    async def _mutate_runner():
        db_session.add(
            LocalRunner(
                project_id=project.id,
                token_hash="c" * 64,
                hostname="desk-new",
                status="active",
                last_seen_at=now,
                capabilities={"task_types": "all", "agent_available": True},
            )
        )

    await _assert_flips("runner", _mutate_runner)

    # 3. A new ACTIVE compute job (count + max updated_at fingerprint).
    async def _mutate_compute():
        db_session.add(
            ComputeJob(
                project_id=project.id,
                backend="local",
                status=ComputeJobStatus.RUNNING,
                started_at=now,
                updated_at=now,
            )
        )

    await _assert_flips("compute_job", _mutate_compute)

    # 4. A new PARKED-task wake (active wake count fingerprint).
    parked_task = Task(
        project_id=project.id,
        title="Parked on compute",
        payloads=[{"type": "compute_experiment"}],
        status=TaskStatus.PARKED,
    )
    db_session.add(parked_task)
    await db_session.commit()
    await db_session.refresh(parked_task)

    async def _mutate_wake():
        db_session.add(
            TaskWakeRegistration(
                task_id=parked_task.id,
                project_id=project.id,
                wake_source_kind="compute_job_terminal",
                wake_source_ref="job-xyz",
                wake_action=WakeAction.FINALIZE,
                delivery_state=WakeDeliveryState.PENDING,
                requested_at=now,
                deadline_at=now + timedelta(hours=1),
            )
        )

    await _assert_flips("wake", _mutate_wake)

    # 5. A new project message (event feed / command-job / preview fingerprint).
    async def _mutate_message():
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                message_type=MessageType.NOTE,
                content="operator note",
                from_type="user",
                status="open",
            )
        )

    await _assert_flips("message", _mutate_message)

    # 6. An open ReleaseVersion (model-integrity / sha-freshness fingerprint).
    async def _mutate_open_version():
        db_session.add(
            ReleaseVersion(
                project_id=project.id, version_number=1, status="open"
            )
        )

    await _assert_flips("open_version", _mutate_open_version)

    # 7. A Direction row's criterion_keys. model_integrity / criterion_links are
    #    computed from Direction rows (NOT the project.directions blob the narrow
    #    token folds in), so editing them must flip the broad revision.
    direction = Direction(
        project_id=project.id,
        direction_key="d-fp-1",
        status="active",
        text="fingerprint direction",
        criterion_keys=["crit-a"],
    )
    db_session.add(direction)
    await db_session.commit()
    await db_session.refresh(direction)

    async def _mutate_direction_keys():
        direction.criterion_keys = ["crit-a", "crit-b"]
        db_session.add(direction)

    await _assert_flips("direction_criterion_keys", _mutate_direction_keys)


@pytest.mark.asyncio
async def test_fingerprint_live_runner_count_flips_on_time_derived_staleness(
    client: AsyncClient, db_session, mocker
):
    """A live runner going stale is a purely time-derived transition: last_seen_at
    (and thus runner_max_seen / runner_count) never moves, yet is_live flips to
    False once the heartbeat ages past _RUNNER_LIVENESS_SECONDS. The broad-revision
    fingerprint must catch it via live_runner_count so no false 304 pins the
    resident-session picker / availability on a now-offline runner."""
    from app.routers.admin_project_strategic_session import (
        _RUNNER_LIVENESS_SECONDS,
        _compute_strategic_state_fingerprints,
    )

    project = await _make_project(db_session)
    seen_at = _utcnow() - timedelta(seconds=100)
    runner = LocalRunner(
        project_id=project.id,
        token_hash="d" * 64,
        hostname="desk-live",
        status="active",
        last_seen_at=seen_at,
        capabilities={"task_types": "all", "agent_available": True},
    )
    db_session.add(runner)
    await db_session.commit()

    fp_live = await _compute_strategic_state_fingerprints(
        db_session, project.id, TEST_USER_ID
    )
    assert fp_live["live_runner_count"] == 1
    assert fp_live["runner_count"] == 1

    # Advance the wall clock past the liveness window WITHOUT touching the DB.
    mocker.patch(
        "app.routers.admin_project_strategic_session._utcnow",
        return_value=seen_at + timedelta(seconds=_RUNNER_LIVENESS_SECONDS + 30),
    )
    fp_stale = await _compute_strategic_state_fingerprints(
        db_session, project.id, TEST_USER_ID
    )
    # DB-derived probes are frozen; only the time-derived liveness count drops —
    # which is exactly what must flip the broad revision.
    assert fp_stale["runner_count"] == fp_live["runner_count"]
    assert fp_stale["runner_max_seen"] == fp_live["runner_max_seen"]
    assert fp_stale["live_runner_count"] == 0


@pytest.mark.asyncio
async def test_attention_fingerprints_cover_completion_debt_and_time_boundary(
    db_session, mocker
):
    """Attention-only durable and clock changes must defeat a held revision.

    Completion debt is not represented by the message/task fingerprints.  Its
    24-hour stale threshold likewise changes attention without a database write,
    so both must have a cheap Phase-1 fingerprint.
    """
    from app.routers.admin_project_strategic_session import (
        _compute_strategic_state_fingerprints,
    )

    project = await _make_project(db_session)
    now = _utcnow()
    before = await _compute_strategic_state_fingerprints(
        db_session, project.id, TEST_USER_ID
    )
    assert before["completion_debt_count"] == 0
    assert before["attention_next_time_boundary"] is None

    debt = CompletionDebtEntry(
        project_id=project.id,
        kind="retirement_residual",
        title="Debt about to become operator attention",
        status="open",
        created_at=now - timedelta(hours=23, minutes=59),
    )
    db_session.add(debt)
    await db_session.commit()
    with_debt = await _compute_strategic_state_fingerprints(
        db_session, project.id, TEST_USER_ID
    )
    assert with_debt["completion_debt_count"] == 1
    assert with_debt["completion_debt_max_updated"] is not None
    assert with_debt["attention_next_time_boundary"] is not None

    # Crossing the debt's threshold leaves all durable rows untouched, but the
    # upcoming-boundary stamp disappears so the state revision cannot 304.
    mocker.patch(
        "app.routers.admin_project_strategic_session._utcnow",
        return_value=now + timedelta(minutes=2),
    )
    after_threshold = await _compute_strategic_state_fingerprints(
        db_session, project.id, TEST_USER_ID
    )
    assert after_threshold["completion_debt_count"] == with_debt["completion_debt_count"]
    assert after_threshold["completion_debt_max_updated"] == with_debt["completion_debt_max_updated"]
    assert after_threshold["attention_next_time_boundary"] != with_debt["attention_next_time_boundary"]


@pytest.mark.asyncio
async def test_strategic_state_broad_revision_delivers_cold_to_warm_currency(
    client: AsyncClient, db_session, mocker
):
    """The broad-revision 304 skip must NOT mask the sha_freshness cold→warm
    transition. sha_freshness is resolved into the out-of-band _CURRENCY_CACHE
    (never the DB), so after a cold poll warms the cache, the next poll carrying
    the cold poll's revision must RE-ASSEMBLE (200 with the resolved verdict),
    not return an early 304 that pins the banner on 'unknown'."""
    import asyncio
    from app.routers.admin_project_strategic_session import _CURRENCY_CACHE

    project = await _make_project(db_session)
    sha = "feedface" * 5
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="open",
        opened_at=_utcnow(),
        last_qualification={"candidate_sha": sha, "status": "not_promoted"},
    )
    db_session.add(version)
    await db_session.commit()

    mocker.patch(
        "app.strategic_spawning.get_project_integration_branch",
        return_value="main",
    )
    mocker.patch(
        "app.strategic_spawning._resolve_candidate_currency",
        new=AsyncMock(
            return_value={"current": True, "candidate_sha": sha, "head_sha": sha}
        ),
    )
    mocker.patch.dict(
        "app.routers.admin_project_strategic_session._CURRENCY_CACHE",
        {},
        clear=True,
    )

    bg_tasks: list[asyncio.Task] = []
    original_create_task = asyncio.create_task

    def _capture(coro, **kw):
        t = original_create_task(coro, **kw)
        bg_tasks.append(t)
        return t

    mocker.patch("asyncio.create_task", side_effect=_capture)

    # Poll 1 — cold cache: unknown verdict, capture the broad revision.
    r1 = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["sha_freshness"]["verdict"] == "unknown"
    rev1 = body1["state_revision"]

    # Warm the cache out-of-band, exactly as the background resolver does.
    await asyncio.gather(*bg_tasks, return_exceptions=True)
    cache_key = (str(project.id), "main", sha)
    assert cache_key in _CURRENCY_CACHE

    # Poll 2 — identical DB state, carrying poll-1's revision. Must NOT 304: the
    # warmed verdict has to be delivered.
    r2 = await client.get(
        f"/api/admin/projects/{project.id}/strategic-state?state_revision={rev1}"
    )
    assert r2.status_code == 200, (
        "cold→warm currency transition was masked by a false broad-revision 304"
    )
    body2 = r2.json()
    assert body2["sha_freshness"]["verdict"] != "unknown"
    assert body2["state_revision"] != rev1


@pytest.mark.asyncio
async def test_strategic_state_non_matching_revision_triggers_full_build(
    client: AsyncClient, db_session, mocker
):
    """When the client sends a stale/non-matching state_revision, the endpoint
    performs the full context-evidence load and package build (context_package present)."""
    project = await _make_project(db_session)

    # Mock the expensive steps to confirm they ARE called on a non-matching revision
    evidence_spy = mocker.patch(
        "app.routers.admin_project_strategic_session.load_strategic_context_evidence",
        new_callable=AsyncMock,
        return_value={},
    )
    build_spy = mocker.patch(
        "app.routers.admin_project_strategic_session.build_strategic_context_package_summary",
        return_value={"id": f"strategic-context:{project.id}", "sources": []},
    )

    # Call with a clearly bogus revision token
    resp = await client.get(
        f"/api/admin/projects/{project.id}/strategic-state?state_revision=deadbeef12345678"
    )
    assert resp.status_code == 200
    data = resp.json()

    # The expensive functions must have been called
    evidence_spy.assert_called_once()
    build_spy.assert_called_once()

    # context_package is present
    assert data.get("context_package") is not None


@pytest.mark.asyncio
async def test_strategic_state_absent_revision_triggers_full_build(
    client: AsyncClient, db_session, mocker
):
    """When the client sends no state_revision (first-ever poll or backward compat),
    the endpoint performs the full context-evidence load and package build."""
    project = await _make_project(db_session)

    evidence_spy = mocker.patch(
        "app.routers.admin_project_strategic_session.load_strategic_context_evidence",
        new_callable=AsyncMock,
        return_value={},
    )
    build_spy = mocker.patch(
        "app.routers.admin_project_strategic_session.build_strategic_context_package_summary",
        return_value={"id": f"strategic-context:{project.id}", "sources": []},
    )

    # No state_revision parameter
    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    data = resp.json()

    evidence_spy.assert_called_once()
    build_spy.assert_called_once()

    assert data.get("context_package") is not None


@pytest.mark.asyncio
async def test_strategic_state_sets_etag_header_on_full_200(
    client: AsyncClient, db_session
):
    """A full 200 response carries a strong ETag header covering the whole body."""
    project = await _make_project(db_session)

    resp = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp.status_code == 200
    etag = resp.headers.get("ETag")
    assert etag is not None, "full 200 must set an ETag header"
    # Strong validator: a double-quoted hex sha256 digest.
    assert etag.startswith('"') and etag.endswith('"'), "ETag must be quoted"
    assert len(etag.strip('"')) == 64, "ETag must be a full sha256 hex digest"


@pytest.mark.asyncio
async def test_strategic_state_matching_revision_returns_304(
    client: AsyncClient, db_session
):
    """ACCEPTANCE: a re-poll whose If-None-Match matches the current full-body
    ETag returns HTTP 304 Not Modified with an EMPTY body, eliminating the
    redundant full-body transmission on an unchanged poll."""
    project = await _make_project(db_session)

    # First poll: full 200 body + ETag header.
    resp1 = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp1.status_code == 200
    etag = resp1.headers.get("ETag")
    assert etag is not None
    full_bytes = len(resp1.content)
    assert full_bytes > 0

    # Second poll with the held ETag and NOTHING changed → 304, empty body.
    resp2 = await client.get(
        f"/api/admin/projects/{project.id}/strategic-state",
        headers={"If-None-Match": etag},
    )
    assert resp2.status_code == 304, (
        f"unchanged poll must 304; got {resp2.status_code} body={resp2.text[:200]!r}"
    )
    # Measurement: the 304 body is empty vs the full 200 body.
    assert len(resp2.content) == 0, "304 must have an empty body"
    # The 304 echoes the ETag so the client can keep reusing it.
    assert resp2.headers.get("ETag") == etag
    # Transmission reduction: ~100% of the body bytes are eliminated.
    reduction = 1.0 - (len(resp2.content) / full_bytes)
    assert reduction > 0.99, (
        f"304 must eliminate ~all body bytes (full={full_bytes}, reduction={reduction:.4f})"
    )


@pytest.mark.asyncio
async def test_strategic_state_nonmatching_if_none_match_returns_full_200(
    client: AsyncClient, db_session
):
    """A stale/bogus If-None-Match must NOT 304 — the client gets the full body
    and a fresh ETag."""
    project = await _make_project(db_session)

    resp = await client.get(
        f"/api/admin/projects/{project.id}/strategic-state",
        headers={"If-None-Match": '"deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"'},
    )
    assert resp.status_code == 200
    assert len(resp.content) > 0
    assert resp.json().get("project_id") == str(project.id)
    assert resp.headers.get("ETag") is not None


@pytest.mark.asyncio
async def test_strategic_state_volatile_field_change_busts_etag(
    client: AsyncClient, db_session
):
    """CRITICAL INVARIANT: a change to a field the NARROW state_revision does not
    cover (here the operator workbench state) must change the full-body ETag so a
    held token no longer 304s — no field update is ever silently suppressed."""
    project = await _make_project(db_session)

    resp1 = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp1.status_code == 200
    etag1 = resp1.headers.get("ETag")
    assert etag1 is not None
    assert resp1.json()["workbench"]["active_surface"] != "runtime"

    # Mutate the workbench active_surface — this lives in the `workbench` body
    # field, which the narrow context-package revision token does NOT hash.
    db_session.add(
        MessagesNoisePrefs(
            user_id=TEST_USER_ID,
            project_id=project.id,
            prefs={
                "_strategic_workbench": {
                    "version": 1,
                    "interaction_mode": "continuous",
                    "active_surface": "runtime",
                    "selected_refs": [],
                },
            },
        )
    )
    await db_session.commit()

    # Re-poll with the OLD ETag: the body changed, so the server must NOT 304.
    resp2 = await client.get(
        f"/api/admin/projects/{project.id}/strategic-state",
        headers={"If-None-Match": etag1},
    )
    assert resp2.status_code == 200, "a changed body must never be suppressed by a 304"
    assert resp2.json()["workbench"]["active_surface"] == "runtime"
    etag2 = resp2.headers.get("ETag")
    assert etag2 is not None and etag2 != etag1, "ETag must change when the body changes"

    # And the fresh ETag now 304s on an unchanged re-poll.
    resp3 = await client.get(
        f"/api/admin/projects/{project.id}/strategic-state",
        headers={"If-None-Match": etag2},
    )
    assert resp3.status_code == 304


@pytest.mark.asyncio
async def test_strategic_state_context_package_only_built_on_full_200(
    client: AsyncClient, db_session
):
    """The narrow context-package build-skip has been retired by the broad-revision
    assembly-skip: a reached (non-304) response ALWAYS carries a built
    context_package, and a matching revision 304s outright (no partial
    context_package=None body). Confirms the full context package is present on a
    first-load 200 and that a matching revision never returns a null-package 200."""
    project = await _make_project(db_session)

    resp1 = await client.get(f"/api/admin/projects/{project.id}/strategic-state")
    assert resp1.status_code == 200
    revision = resp1.json().get("state_revision")
    assert revision is not None
    assert resp1.json().get("context_package") is not None, (
        "a reached 200 must always carry a built context package"
    )

    # A matching revision 304s — there is no longer a build-skipped null-package
    # 200 intermediate state.
    resp2 = await client.get(
        f"/api/admin/projects/{project.id}/strategic-state?state_revision={revision}"
    )
    assert resp2.status_code == 304
    assert len(resp2.content) == 0


@pytest.mark.asyncio
async def test_compute_strategic_state_revision_deterministic(
    db_session,
):
    """_compute_strategic_state_revision produces the same 16-char hex digest
    given the same inputs, regardless of call timing or order."""
    from app.routers.admin_project_strategic_session import (
        _compute_strategic_state_revision,
    )

    project = await _make_project(db_session)
    base_kwargs = {
        "active_cycle_summary": None,
        "active_task": None,
        "latest_cycle_summary": None,
        "running_update_summary": {"excerpt": None, "char_count": 0},
        "problem_model_summary": {
            "active_gap_count": 1,
            "capability_count": 2,
            "pending_confirmation_count": 1,
            "top_gap": {"id": "g-1"},
        },
        "directions_summary": {"total_count": 2, "active_count": 2, "paused_count": 0},
        "message_counts": {"open": 0, "linked": 0},
        "task_counts": {
            "queued": 0,
            "running": 0,
            "evaluating": 0,
            "blocked": 0,
            "parked": 0,
            "failed": 0,
            "completed": 0,
            "cancelled": 0,
            "total": 0,
        },
        "attention": None,
    }

    # Same inputs → same output every time
    rev1 = _compute_strategic_state_revision(project, **base_kwargs)
    rev2 = _compute_strategic_state_revision(project, **base_kwargs)
    assert rev1 == rev2, "deterministic: same inputs must produce same revision"

    # Different project name → different output
    project.name = "Different Project"
    rev3 = _compute_strategic_state_revision(project, **base_kwargs)
    assert rev3 != rev1, (
        "changing project.name must produce a different revision"
    )

    # Different active_cycle_summary → different output
    project.name = "Strategic State"
    rev4 = _compute_strategic_state_revision(project, **base_kwargs)
    assert rev4 == rev1, "reverting project.name must restore original revision"
    diff_kwargs = {**base_kwargs, "active_cycle_summary": {"id": "sc-new"}}
    rev5 = _compute_strategic_state_revision(project, **diff_kwargs)
    assert rev5 != rev4, (
        "changing active_cycle_summary must produce a different revision"
    )

    # fingerprints defaults to None → identical to omitting it (backward compat).
    rev6 = _compute_strategic_state_revision(
        project, **base_kwargs, fingerprints=None
    )
    assert rev6 == rev1, "fingerprints=None must not change the revision"

    # A non-empty fingerprints map changes the token...
    fp = {"runner_count": 1, "runner_max_seen": "2026-07-13T00:00:00"}
    rev7 = _compute_strategic_state_revision(
        project, **base_kwargs, fingerprints=fp
    )
    assert rev7 != rev1, "a non-empty fingerprints map must change the revision"

    # ...and a mutated fingerprint value changes it again (guards false 304s at
    # the hash layer).
    fp2 = {**fp, "runner_count": 2}
    rev8 = _compute_strategic_state_revision(
        project, **base_kwargs, fingerprints=fp2
    )
    assert rev8 != rev7, (
        "mutating a fingerprint value must change the revision"
    )


def test_task_summary_caps_latest_status_message():
    """_task_summary enforces the declared STATUS_LATEST_MESSAGE_MAX cap on the
    primary strategic status message it surfaces. This exposure was previously
    unbounded, unlike the sibling _task_focus_item which already compacted the
    same field."""
    from app.strategic_constants import STATUS_LATEST_MESSAGE_MAX
    from app.strategic_state_read_model import _task_summary
    from testing.factories import make_task

    oversized = "x" * (STATUS_LATEST_MESSAGE_MAX + 500)
    task = make_task(
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "create"}],
        latest_status_message=oversized,
    )
    summary = _task_summary(task)
    assert summary is not None
    msg = summary["latest_status_message"]
    assert msg is not None
    # _compact_text truncates to exactly max_chars (max_chars-1 chars + ellipsis).
    assert len(msg) == STATUS_LATEST_MESSAGE_MAX
    assert msg.endswith("…")
