import uuid

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.models import Project, ProjectMessage
from app.work_session_command_execution import (
    build_work_session_command_jobs,
    execute_work_session_command,
)

TEST_USER_ID = "test-user-123"


async def _make_project(session) -> Project:
    project = Project(name="Command Receipts", user_id=TEST_USER_ID)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


def _dismiss_alert_command(project_id: uuid.UUID, alert_id: uuid.UUID) -> dict:
    return {
        "id": f"attention.alert.dismiss:{alert_id}",
        "label": "Dismiss alert: Runner offline",
        "enabled": True,
        "target": {
            "surface": "runtime",
            "project_id": str(project_id),
            "section": "health",
            "alert_id": str(alert_id),
            "action": "dismiss_alert",
            "secret": "must-not-leak",
        },
        "refetch_surfaces": ["runtime", "strategic_session"],
        "mutation": {
            "method": "POST",
            "path": f"/api/admin/alerts/{alert_id}/dismiss",
            "body": {"token": "must-not-leak"},
        },
    }


@pytest.mark.asyncio
async def test_failed_allowlisted_command_persists_bounded_receipt_and_jobs(
    db_session,
):
    project = await _make_project(db_session)
    alert_id = uuid.uuid4()
    command = _dismiss_alert_command(project.id, alert_id)

    result = await execute_work_session_command(
        db_session,
        project_id=project.id,
        user_id=TEST_USER_ID,
        command=command,
        idempotency_key="failed-alert-dismiss-1",
    )

    assert result["status"] == "failed"
    assert result["applied"] is False
    assert result["receipt"]["status"] == "failed"
    assert result["receipt"]["result"] == {
        "kind": "command_failed",
        "status_code": 404,
        "error": "Alert not found",
    }
    assert result["receipt"]["target"] == {
        "surface": "runtime",
        "project_id": str(project.id),
        "section": "health",
        "alert_id": str(alert_id),
        "action": "dismiss_alert",
    }

    receipt = await db_session.get(ProjectMessage, uuid.UUID(result["receipt"]["id"]))
    assert receipt is not None
    command_context = receipt.context["operator_command"]
    assert receipt.context["event_type"] == "operator_command"
    assert command_context["status"] == "failed"
    assert command_context["operator_user_id"] == TEST_USER_ID
    assert "Alert not found" in receipt.content
    assert "must-not-leak" not in str(receipt.context)
    assert "mutation" not in str(receipt.context)

    jobs = await build_work_session_command_jobs(
        db_session,
        project.id,
        user_id=TEST_USER_ID,
    )
    assert jobs["items"][0]["id"] == str(receipt.id)
    assert jobs["items"][0]["status"] == "failed"
    assert jobs["items"][0]["result"]["error"] == "Alert not found"

    duplicate = await execute_work_session_command(
        db_session,
        project_id=project.id,
        user_id=TEST_USER_ID,
        command=command,
        idempotency_key="failed-alert-dismiss-1",
    )
    assert duplicate["status"] == "duplicate"
    assert duplicate["receipt"]["id"] == str(receipt.id)
    assert duplicate["receipt"]["status"] == "failed"

    other_command = _dismiss_alert_command(project.id, uuid.uuid4())
    with pytest.raises(HTTPException) as exc_info:
        await execute_work_session_command(
            db_session,
            project_id=project.id,
            user_id=TEST_USER_ID,
            command=other_command,
            idempotency_key="failed-alert-dismiss-1",
        )
    assert exc_info.value.status_code == 409

    receipts = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
    ).scalars().all()
    assert len(receipts) == 1


@pytest.mark.asyncio
async def test_malformed_or_non_allowlisted_commands_do_not_persist_receipts(
    db_session,
):
    project = await _make_project(db_session)
    unsupported_target_alert = _dismiss_alert_command(project.id, uuid.uuid4())
    unsupported_target_alert["target"] = {
        "surface": "debug",
        "project_id": str(project.id),
        "section": "raw",
    }
    commands = [
        (
            {
                "id": "context.canvas.open",
                "label": "Open context canvas",
                "enabled": True,
                "target": {"surface": "project", "section": "problem-model"},
                "mutation": {"method": "POST", "path": "/unsafe", "body": {}},
            },
            "non-allowlisted-command",
        ),
        (
            {
                "id": "attention.messages.mark_seen",
                "label": "Mark messages seen",
                "enabled": True,
                "target": {
                    "surface": "messages",
                    "project_id": str(project.id),
                    "action": "mark_seen",
                },
                "mutation": {
                    "method": "PUT",
                    "path": "/api/admin/messages/last-seen",
                    "body": {},
                },
            },
            "malformed-mark-seen",
        ),
        (
            {
                "id": "session.runner.prefer.runner-a",
                "label": "Prefer runner A",
                "enabled": True,
                "target": {
                    "surface": "runtime",
                    "project_id": str(project.id),
                    "section": "devices",
                    "action": "prefer_runner",
                    "runner_id": "runner-b",
                },
                "mutation": {
                    "method": "POST",
                    "path": f"/api/admin/projects/{project.id}/strategic-session",
                    "body": {"workbench": {"preferred_runner_id": "runner-a"}},
                },
            },
            "mismatched-runner-preference",
        ),
        (
            unsupported_target_alert,
            "unsupported-alert-target",
        ),
    ]

    for command, idem_key in commands:
        with pytest.raises(HTTPException):
            await execute_work_session_command(
                db_session,
                project_id=project.id,
                user_id=TEST_USER_ID,
                command=command,
                idempotency_key=idem_key,
            )

    receipts = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
    ).scalars().all()
    assert receipts == []
