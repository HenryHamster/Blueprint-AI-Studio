"""Focused tests for Fast's bounded canonical live-work authority."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy.orm.attributes import flag_modified

from app.models import (
    MessagesNoisePrefs,
    Project,
    ReleaseDeployArtifact,
    ReleaseVersion,
    Task,
    TaskStatus,
)
from app.strategic_fast_live_work import (
    _bounded_result_evidence_refs,
    _fast_live_task_statement,
    _fast_live_work_authority,
    _fast_recent_terminal_task_statement,
    _fast_result_correlated_document_statement,
    _fast_result_correlated_release_statement,
    _fast_result_correlated_task_statement,
    _fast_release_artifact_statement,
    _fast_release_version_statement,
)
from app.time_utils import _utcnow
from app.workbench_state import WORKBENCH_STATE_KEY


async def _make_resident_poll_worker(
    db_session, *, generation: int = 2, session_id: str = "resident-session"
):
    project = Project(name="Fast Live Work", user_id="test-user-fast-live-work")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        payloads=[{}],
        vm_instance_id="agent-vm-fast-live-work",
        context={
            "_resident_session": True,
            "strategic_session_id": session_id,
            "_resident_worker_generation": generation,
        },
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    project.vision = {
        "_resident_strategic_session": {
            "session_id": session_id,
            "task_id": str(task.id),
            "worker_generation": generation,
            "status": "active",
        }
    }
    db_session.add(project)
    await db_session.commit()
    return project, task


def test_fast_live_task_projection_never_selects_complete_context_json():
    project_id = uuid.uuid4()
    resident_task_id = uuid.uuid4()
    live_statement = _fast_live_task_statement(
        project_id=project_id,
        resident_task_id=resident_task_id,
    )
    terminal_statement = _fast_recent_terminal_task_statement(
        project_id=project_id,
        resident_task_id=resident_task_id,
    )
    correlated_task_statement = _fast_result_correlated_task_statement(
        project_id=project_id,
        resident_task_id=resident_task_id,
        task_ids=[uuid.uuid4()],
    )
    correlated_document_statement = _fast_result_correlated_document_statement(
        project_id=project_id,
        document_ids=[uuid.uuid4()],
    )
    release_statement = _fast_release_version_statement(project_id=project_id)
    correlated_release_statement = _fast_result_correlated_release_statement(
        project_id=project_id,
        version_ids=[uuid.uuid4()],
        sha_refs=["a" * 40],
    )
    artifact_statement = _fast_release_artifact_statement(
        version_ids=[uuid.uuid4()]
    )

    live_selected_names = [
        description["name"] for description in live_statement.column_descriptions
    ]
    assert live_selected_names == [
        "id",
        "title",
        "status",
        "created_by",
        "origination",
        "direction_id",
        "parent_strategic_task_id",
        "strategic_work_order_id",
        "latest_status_message",
        "blocked_reason",
        "recheck_after",
        "updated_at",
    ]
    terminal_selected_names = [
        description["name"]
        for description in terminal_statement.column_descriptions
    ]
    assert terminal_selected_names == [
        "id",
        "title",
        "status",
        "created_by",
        "origination",
        "direction_id",
        "parent_strategic_task_id",
        "strategic_work_order_id",
        "completed_at",
        "updated_at",
    ]
    assert "context" not in live_selected_names
    assert "context" not in terminal_selected_names
    correlated_task_selected_names = [
        description["name"]
        for description in correlated_task_statement.column_descriptions
    ]
    assert correlated_task_selected_names == [
        "id",
        "title",
        "status",
        "created_by",
        "origination",
        "direction_id",
        "parent_strategic_task_id",
        "strategic_work_order_id",
        "latest_status_message",
        "blocked_reason",
        "recheck_after",
        "completed_at",
        "updated_at",
    ]
    assert "context" not in correlated_task_selected_names
    assert [
        description["name"]
        for description in correlated_document_statement.column_descriptions
    ] == [
        "id",
        "source_task_id",
        "source_kind",
        "title",
        "summary",
        "topic",
        "direction_id",
        "head_sha",
        "lifecycle_status",
        "created_at",
        "updated_at",
    ]
    assert [
        description["name"] for description in release_statement.column_descriptions
    ] == [
        "id",
        "project_id",
        "version_number",
        "phase",
        "status",
        "base_sha",
        "promoted_sha",
        "promoted_at",
        "serving_status",
        "served_sha",
        "served_at",
        "deploy_confirm_deadline",
        "deploy_failure_reason",
        "active_qualification_attempt_id",
        "qualification_attempts",
        "updated_at",
    ]
    assert [
        description["name"]
        for description in correlated_release_statement.column_descriptions
    ] == [
        description["name"]
        for description in release_statement.column_descriptions
    ]
    assert [
        description["name"] for description in artifact_statement.column_descriptions
    ] == [
        "id",
        "version_id",
        "artifact_kind",
        "serving_status",
        "served_identifier",
        "confirmation_deadline",
        "reason_code",
        "served_at",
        "updated_at",
    ]


def test_result_evidence_ref_extraction_scans_full_bounded_summary():
    late_task_id = uuid.uuid4()
    late_sha = "b" * 40
    ids, shas, ids_have_more, shas_have_more = _bounded_result_evidence_refs(
        f"{'x' * 19_900} {late_task_id} {late_sha}"
    )

    assert ids == [late_task_id]
    assert shas == [late_sha]
    assert ids_have_more is False
    assert shas_have_more is False

    _, _, ids_have_more, shas_have_more = _bounded_result_evidence_refs(
        f"{'x' * 20_001} {uuid.uuid4()} {'c' * 40}"
    )
    assert ids_have_more is True
    assert shas_have_more is True


@pytest.mark.asyncio
async def test_fast_live_role_contract_disables_private_autonomy_in_chat(db_session):
    project, task = await _make_resident_poll_worker(db_session)

    authority = await _fast_live_work_authority(
        db_session,
        project=project,
        resident_task_id=task.id,
    )

    assert authority["role_contract"] == {
        "public_reply_owner": "fast",
        "workday_request_receipt": "private_consultation_only",
        "interaction_mode": "chat",
        "private_workday_autonomy_configured": False,
        "private_workday_autonomy": "disabled",
        "private_workday_autonomy_reason_code": "chat_or_disabled",
        "project_autonomous_enabled": True,
    }
    assert authority["resident"]["interaction_mode"] == "chat"
    assert authority["resident"]["private_autonomy_configured"] is False
    assert authority["resident"]["private_autonomy_enabled"] is False

    now = _utcnow().isoformat()
    project.autonomous_enabled = False
    db_session.add(project)
    db_session.add(
        MessagesNoisePrefs(
            user_id=project.user_id,
            project_id=project.id,
            prefs={
                WORKBENCH_STATE_KEY: {
                    "version": 1,
                    "interaction_mode": "continuous",
                    "active_surface": "strategic",
                    "selected_refs": [],
                    "session_started_at": now,
                    "last_interaction_at": now,
                }
            },
        )
    )
    await db_session.commit()

    configured_without_activity = await _fast_live_work_authority(
        db_session,
        project=project,
        resident_task_id=task.id,
    )
    assert configured_without_activity["resident"]["interaction_mode"] == "continuous"
    assert configured_without_activity["role_contract"] == {
        "public_reply_owner": "fast",
        "workday_request_receipt": "private_consultation_only",
        "interaction_mode": "continuous",
        "private_workday_autonomy_configured": True,
        "private_workday_autonomy": "unknown",
        "private_workday_autonomy_reason_code": "activity_unreported",
        "project_autonomous_enabled": False,
    }
    assert configured_without_activity["resident"][
        "private_autonomy_configured"
    ] is True
    assert configured_without_activity["resident"]["private_autonomy_enabled"] is False

    project.vision["_resident_strategic_session"]["activity"] = {
        "autonomy_state": "active",
        "autonomy_reason_code": None,
    }
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()
    currently_active = await _fast_live_work_authority(
        db_session,
        project=project,
        resident_task_id=task.id,
    )
    assert currently_active["role_contract"]["private_workday_autonomy"] == "enabled"
    assert currently_active["role_contract"]["project_autonomous_enabled"] is False
    assert currently_active["resident"]["private_autonomy_enabled"] is True

    project.vision["_resident_strategic_session"]["activity"] = {
        "autonomy_state": "paused",
        "autonomy_reason_code": "authority_not_held",
    }
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()
    currently_paused = await _fast_live_work_authority(
        db_session,
        project=project,
        resident_task_id=task.id,
    )
    assert currently_paused["role_contract"]["private_workday_autonomy"] == "paused"
    assert currently_paused["role_contract"][
        "private_workday_autonomy_reason_code"
    ] == "authority_not_held"
    assert currently_paused["resident"]["private_autonomy_enabled"] is False

    project.vision["_resident_strategic_session"]["stop_requested"] = True
    flag_modified(project, "vision")
    db_session.add(project)
    await db_session.commit()
    currently_unavailable = await _fast_live_work_authority(
        db_session,
        project=project,
        resident_task_id=task.id,
    )
    assert currently_unavailable["role_contract"][
        "private_workday_autonomy_configured"
    ] is True
    assert currently_unavailable["role_contract"][
        "private_workday_autonomy"
    ] == "unavailable"
    assert currently_unavailable["role_contract"][
        "private_workday_autonomy_reason_code"
    ] == "resident_inactive"


@pytest.mark.asyncio
async def test_fast_live_work_marks_bounded_collections_as_truncated(db_session):
    project, resident_task = await _make_resident_poll_worker(db_session)
    project.directions = [
        {
            "id": f"direction-{index:02d}",
            "status": "active",
            "text": f"Live direction {index}",
        }
        for index in range(13)
    ]
    live_tasks = [
        Task(
            project_id=project.id,
            title=f"Live task {index}",
            status=TaskStatus.QUEUED,
            context={"direction_id": f"direction-{index:02d}"},
        )
        for index in range(13)
    ]
    active_resident_generation = Task(
        project_id=project.id,
        title="Resident generation active",
        status=TaskStatus.IN_PROGRESS,
        context={"_resident_session": True},
        updated_at=_utcnow() + timedelta(hours=1),
    )
    terminal_tasks = [
        Task(
            project_id=project.id,
            title=f"Terminal task {index}",
            status=TaskStatus.COMPLETED,
            completed_at=_utcnow() - timedelta(minutes=index),
            context={"direction_id": f"direction-{index:02d}"},
            updated_at=_utcnow() - timedelta(hours=1, minutes=index),
        )
        for index in range(7)
    ]
    newer_resident_generations = [
        Task(
            project_id=project.id,
            title=f"Resident generation {index}",
            status=TaskStatus.COMPLETED,
            completed_at=_utcnow() + timedelta(minutes=index),
            context={"_resident_session": True},
            updated_at=_utcnow() + timedelta(minutes=index),
        )
        for index in range(7)
    ]
    served_sha = "a" * 40
    release_versions = [
        ReleaseVersion(
            project_id=project.id,
            version_number=index,
            status="promoted",
            promoted_sha=served_sha if index == 5 else f"{index}" * 40,
            promoted_at=_utcnow() - timedelta(days=5 - index),
            serving_status="serving_confirmed" if index == 5 else "unconfirmed",
            served_sha=served_sha if index == 5 else None,
            served_at=_utcnow() if index == 5 else None,
        )
        for index in range(1, 6)
    ]
    served_release_artifact = ReleaseDeployArtifact(
        version_id=release_versions[-1].id,
        artifact_kind="promotion_merge",
        serving_status="serving_confirmed",
        served_identifier=served_sha,
        served_at=_utcnow(),
    )
    db_session.add(project)
    db_session.add_all(
        [
            *live_tasks,
            active_resident_generation,
            *terminal_tasks,
            *newer_resident_generations,
            *release_versions,
            served_release_artifact,
        ]
    )
    await db_session.commit()

    authority = await _fast_live_work_authority(
        db_session,
        project=project,
        resident_task_id=resident_task.id,
    )

    assert len(authority["active_directions"]) == 12
    assert len(authority["active_tasks"]) == 12
    assert len(authority["recent_terminal_tasks"]) == 6
    assert len(authority["release_deployments"]) == 4
    assert {
        task["status"] for task in authority["recent_terminal_tasks"]
    } == {"COMPLETED"}
    assert all(
        not task["title"].startswith("Resident generation")
        for task in authority["recent_terminal_tasks"]
    )
    assert all(
        task["id"] != str(active_resident_generation.id)
        for task in authority["active_tasks"]
    )
    assert any(
        task["id"] == str(terminal_tasks[0].id)
        for task in authority["recent_terminal_tasks"]
    )
    assert authority["collection_metadata"]["active_directions"] == {
        "returned_count": 12,
        "has_more": True,
    }
    assert authority["collection_metadata"]["active_tasks"] == {
        "returned_count": 12,
        "has_more": True,
    }
    assert authority["collection_metadata"]["recent_terminal_tasks"] == {
        "returned_count": 6,
        "has_more": True,
    }
    assert authority["collection_metadata"]["release_deployments"] == {
        "returned_count": 4,
        "has_more": True,
    }
    latest_release = authority["release_deployments"][0]
    assert latest_release["id"] == str(release_versions[-1].id)
    assert latest_release["promoted_sha"] == served_sha
    assert latest_release["served_sha"] == served_sha
    assert latest_release["serving_status"] == "serving_confirmed"
    assert latest_release["required_artifacts"] == [
        {
            "artifact_kind": "promotion_merge",
            "required": True,
            "serving_status": "serving_confirmed",
            "served_identifier": served_sha,
            "served_at": served_release_artifact.served_at.isoformat(),
            "reason_code": None,
        }
    ]
    assert "unlisted id or state is unknown" in authority[
        "collection_metadata"
    ]["absence_rule"]


@pytest.mark.asyncio
async def test_receipts_resolve_exact_project_entities_outside_global_windows(
    db_session,
):
    project, resident_task = await _make_resident_poll_worker(db_session)
    direction_id = "receipt-direction"
    project.directions = [
        {"id": f"newer-{index}", "status": "active", "text": "Churn"}
        for index in range(13)
    ] + [{"id": direction_id, "status": "active", "text": "Exact dispatch"}]
    receipt_task = Task(
        project_id=project.id,
        title="Exact receipt task",
        status=TaskStatus.COMPLETED,
        completed_at=_utcnow() - timedelta(days=30),
        updated_at=_utcnow() - timedelta(days=30),
        context={"direction_id": direction_id},
    )
    other_project = Project(name="Other", user_id="test-user-fast-live-work")
    db_session.add_all([project, receipt_task, other_project])
    await db_session.flush()
    unrelated_task = Task(
        project_id=other_project.id,
        title="Unrelated project task",
        status=TaskStatus.COMPLETED,
        context={},
    )
    db_session.add(unrelated_task)
    await db_session.commit()
    direction_operation_id = uuid.uuid4()
    task_operation_id = uuid.uuid4()
    authority = await _fast_live_work_authority(
        db_session,
        project=project,
        resident_task_id=resident_task.id,
        result_evidence_text=f"mentioned unrelated {unrelated_task.id}",
        consultation_operation_receipts=[
            {
                "operation_id": str(direction_operation_id),
                "entity_kind": "direction",
                "entity_id": direction_id,
                "effect": "created",
            },
            {
                "operation_id": str(task_operation_id),
                "entity_kind": "task",
                "entity_id": str(receipt_task.id),
                "effect": "created",
            },
            {
                "operation_id": str(uuid.uuid4()),
                "entity_kind": "task",
                "entity_id": str(unrelated_task.id),
                "effect": "created",
            },
        ],
    )

    assert direction_id not in {item["id"] for item in authority["active_directions"]}
    assert authority["receipt_correlated_directions"] == [
        {
            "id": direction_id,
            "status": "active",
            "consultation_receipts": [
                {"operation_id": str(direction_operation_id), "effect": "created"}
            ],
            "text": "Exact dispatch",
        }
    ]
    assert authority["receipt_correlated_tasks"][0]["id"] == str(receipt_task.id)
    assert authority["receipt_correlated_tasks"][0]["consultation_receipts"] == [
        {"operation_id": str(task_operation_id), "effect": "created"}
    ]
    assert all(
        item["id"] != str(unrelated_task.id)
        for item in authority["result_correlated_tasks"]
        + authority["receipt_correlated_tasks"]
    )
