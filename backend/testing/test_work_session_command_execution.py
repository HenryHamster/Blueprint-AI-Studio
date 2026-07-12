import uuid

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.models import Alert, LocalRunner, Project, ProjectMessage
from app.messages_noise_prefs_store import get_messages_noise_prefs_row
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
            "body": {},
        },
    }


@pytest.mark.asyncio
async def test_failed_allowlisted_command_persists_bounded_receipt_and_jobs(
    db_session,
):
    project = await _make_project(db_session)
    # Snapshot the id: the failed-command path inside
    # execute_work_session_command rolls back the session, expiring
    # ``project`` — later ``project.id`` lazy-loads would raise
    # MissingGreenlet under real ORM expiry.
    project_id = project.id
    alert_id = uuid.uuid4()
    command = _dismiss_alert_command(project_id, alert_id)

    result = await execute_work_session_command(
        db_session,
        project_id=project_id,
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
        "project_id": str(project_id),
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
        project_id,
        user_id=TEST_USER_ID,
    )
    assert jobs["items"][0]["id"] == str(receipt.id)
    assert jobs["items"][0]["status"] == "failed"
    assert jobs["items"][0]["result"]["error"] == "Alert not found"

    duplicate = await execute_work_session_command(
        db_session,
        project_id=project_id,
        user_id=TEST_USER_ID,
        command=command,
        idempotency_key="failed-alert-dismiss-1",
    )
    assert duplicate["status"] == "duplicate"
    assert duplicate["receipt"]["id"] == str(receipt.id)
    assert duplicate["receipt"]["status"] == "failed"

    other_command = _dismiss_alert_command(project_id, uuid.uuid4())
    with pytest.raises(HTTPException) as exc_info:
        await execute_work_session_command(
            db_session,
            project_id=project_id,
            user_id=TEST_USER_ID,
            command=other_command,
            idempotency_key="failed-alert-dismiss-1",
        )
    assert exc_info.value.status_code == 409

    receipts = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.project_id == project_id)
        )
    ).scalars().all()
    assert len(receipts) == 1


@pytest.mark.asyncio
async def test_executes_mode_command_through_shared_contract(db_session):
    # The interaction vocabulary collapsed to Chat + Workday; exercise the shared
    # contract with the Workday (continuous) mode command.
    project = await _make_project(db_session)
    command = {
        "id": "session.mode.continuous",
        "label": "Workday mode",
        "enabled": True,
        "target": {
            "surface": "messages",
            "project_id": str(project.id),
            "action": "set_continuous_mode",
        },
        "refetch_surfaces": ["strategic_session"],
        "mutation": {
            "method": "POST",
            "path": f"/api/admin/projects/{project.id}/strategic-session",
            "body": {"workbench": {"interaction_mode": "continuous"}},
        },
    }

    result = await execute_work_session_command(
        db_session,
        project_id=project.id,
        user_id=TEST_USER_ID,
        command=command,
        idempotency_key="continuous-mode-1",
    )

    assert result["status"] == "succeeded"
    assert result["applied"] is True
    assert result["receipt"]["command_id"] == "session.mode.continuous"
    prefs = await get_messages_noise_prefs_row(
        db_session,
        TEST_USER_ID,
        project.id,
    )
    assert prefs is not None
    assert prefs.prefs["_strategic_workbench"]["interaction_mode"] == "continuous"


@pytest.mark.asyncio
async def test_drifted_executable_descriptors_do_not_apply_or_persist_receipts(
    db_session,
):
    project = await _make_project(db_session)
    alert = Alert(
        project_id=project.id,
        title="Runner offline",
        message="Runner stopped heartbeating",
    )
    db_session.add(alert)
    await db_session.commit()
    await db_session.refresh(alert)

    commands = [
        (
            {
                "id": "session.mode.check_in",
                "label": "Check-in mode",
                "enabled": True,
                "target": {
                    "surface": "messages",
                    "project_id": str(project.id),
                    "action": "set_check_in_mode",
                },
                "mutation": {
                    "method": "POST",
                    "path": f"/api/admin/projects/{project.id}/strategic-session?unsafe=1",
                    "body": {"workbench": {"interaction_mode": "check_in"}},
                },
            },
            "drifted-mode-path",
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
                    "body": {
                        "project_id": str(uuid.uuid4()),
                        "timestamp": 123456,
                    },
                },
            },
            "drifted-mark-seen-project",
        ),
        (
            {
                "id": f"attention.alert.dismiss:{alert.id}",
                "label": "Dismiss alert: Runner offline",
                "enabled": True,
                "target": {
                    "surface": "runtime",
                    "project_id": str(project.id),
                    "section": "health",
                    "alert_id": str(uuid.uuid4()),
                    "action": "dismiss_alert",
                },
                "mutation": {
                    "method": "POST",
                    "path": f"/api/admin/alerts/{alert.id}/dismiss",
                    "body": {},
                },
            },
            "drifted-alert-target",
        ),
        (
            {
                "id": f"attention.alert.dismiss:{alert.id}",
                "label": "Dismiss alert: Runner offline",
                "enabled": True,
                "target": {
                    "surface": "runtime",
                    "project_id": str(project.id),
                    "section": "health",
                    "alert_id": str(alert.id),
                    "action": "dismiss_alert",
                },
                "mutation": {
                    "method": "POST",
                    "path": f"/api/admin/alerts/{alert.id}/dismiss",
                    "body": {"token": "must-not-leak"},
                },
            },
            "drifted-alert-body",
        ),
    ]

    for command, idempotency_key in commands:
        with pytest.raises(HTTPException) as exc_info:
            await execute_work_session_command(
                db_session,
                project_id=project.id,
                user_id=TEST_USER_ID,
                command=command,
                idempotency_key=idempotency_key,
            )
        assert exc_info.value.status_code == 409

    await db_session.refresh(alert)
    assert alert.dismissed_at is None
    receipts = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
    ).scalars().all()
    assert receipts == []


@pytest.mark.asyncio
async def test_executes_runner_prefer_and_clear_with_stale_clear_guard(db_session):
    project = await _make_project(db_session)
    # Snapshot the id: the stale-clear 409 path inside
    # execute_work_session_command rolls back the session, expiring
    # ``project`` — later ``project.id`` lazy-loads would raise
    # MissingGreenlet under real ORM expiry.
    project_id = project.id
    runner = LocalRunner(
        project_id=project_id,
        token_hash="a" * 64,
        hostname="desk-mini",
        status="active",
    )
    stale_runner = LocalRunner(
        project_id=project_id,
        token_hash="b" * 64,
        hostname="old-desk",
        status="active",
    )
    db_session.add_all([runner, stale_runner])
    await db_session.commit()
    await db_session.refresh(runner)
    await db_session.refresh(stale_runner)
    runner_id = str(runner.id)
    stale_runner_id = str(stale_runner.id)
    prefer_command = {
        "id": f"session.runner.prefer.{runner_id}",
        "label": "Prefer runner A",
        "enabled": True,
        "target": {
            "surface": "runtime",
            "project_id": str(project_id),
            "section": "devices",
            "action": "prefer_runner",
            "runner_id": runner_id,
        },
        "refetch_surfaces": ["strategic_session", "runtime"],
        "mutation": {
            "method": "POST",
            "path": f"/api/admin/projects/{project_id}/strategic-session",
            "body": {"workbench": {"preferred_runner_id": runner_id}},
        },
    }

    prefer_result = await execute_work_session_command(
        db_session,
        project_id=project_id,
        user_id=TEST_USER_ID,
        command=prefer_command,
        idempotency_key="prefer-runner-a-1",
    )

    assert prefer_result["status"] == "succeeded"
    prefs = await get_messages_noise_prefs_row(db_session, TEST_USER_ID, project_id)
    assert prefs is not None
    assert prefs.prefs["_strategic_workbench"]["preferred_runner_id"] == runner_id

    stale_clear_command = {
        "id": "session.runner.clear_preferred",
        "label": "Clear preferred runner",
        "enabled": True,
        "target": {
            "surface": "runtime",
            "project_id": str(project_id),
            "section": "devices",
            "action": "clear_preferred_runner",
            "runner_id": stale_runner_id,
        },
        "refetch_surfaces": ["strategic_session", "runtime"],
        "mutation": {
            "method": "POST",
            "path": f"/api/admin/projects/{project_id}/strategic-session",
            "body": {"workbench": {"preferred_runner_id": None}},
        },
    }
    with pytest.raises(HTTPException) as exc_info:
        await execute_work_session_command(
            db_session,
            project_id=project_id,
            user_id=TEST_USER_ID,
            command=stale_clear_command,
            idempotency_key="clear-runner-stale-1",
        )
    assert exc_info.value.status_code == 409

    prefs = await get_messages_noise_prefs_row(db_session, TEST_USER_ID, project_id)
    assert prefs is not None
    assert prefs.prefs["_strategic_workbench"]["preferred_runner_id"] == runner_id

    clear_command = {
        **stale_clear_command,
        "target": {
            **stale_clear_command["target"],
            "runner_id": runner_id,
        },
    }
    clear_result = await execute_work_session_command(
        db_session,
        project_id=project_id,
        user_id=TEST_USER_ID,
        command=clear_command,
        idempotency_key="clear-runner-a-1",
    )

    assert clear_result["status"] == "succeeded"
    prefs = await get_messages_noise_prefs_row(db_session, TEST_USER_ID, project_id)
    assert prefs is not None
    assert prefs.prefs["_strategic_workbench"].get("preferred_runner_id") is None


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
        (
            {
                "id": "session.mode.monitor",
                "label": "Monitor mode",
                "enabled": True,
                "target": {
                    "surface": "messages",
                    "project_id": str(project.id),
                    "action": "set_monitor_mode",
                },
                "mutation": {
                    "method": "POST",
                    "path": f"/api/admin/projects/{project.id}/strategic-session",
                    "body": {"workbench": {"interaction_mode": "continuous"}},
                },
            },
            "mismatched-session-mode",
        ),
        (
            {
                "id": "session.mode.monitor",
                "label": "Monitor mode",
                "enabled": True,
                "target": {
                    "surface": "runtime",
                    "project_id": str(project.id),
                    "section": "health",
                },
                "mutation": {
                    "method": "POST",
                    "path": f"/api/admin/projects/{project.id}/strategic-session",
                    "body": {"workbench": {"interaction_mode": "monitor"}},
                },
            },
            "mismatched-session-target",
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
