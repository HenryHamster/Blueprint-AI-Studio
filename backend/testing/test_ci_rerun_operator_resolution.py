"""Authenticated operator resolution for indeterminate CI rerun mutations."""

from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app.admin_approval_authority import establish_independent_approval_hold
from app.models import Project, ProjectMember, Task, TaskHistory, TaskStatus


TEST_USER_ID = "test-user-123"


async def _make_timed_out_task(db_session, *, role: str = "member") -> Task:
    project = Project(
        name="CI rerun operator resolution",
        user_id="project-owner",
        github_owner="owner",
        github_repo="repo",
    )
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role=role)
    )
    task = Task(
        project_id=project.id,
        title="Timed-out CI rerun",
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "task"}],
        context={
            "review_loop": {
                "ci_waiting": True,
                "pr_number": 42,
                "head_sha": "operator-resolution-head",
                "ci_last_polled": "2026-01-01T00:00:00",
                "ci_poll_interval": 120,
            },
            "ci_rerun_consumed": 1,
            "ci_rerun_journal": {
                "id": "operator-journal",
                "state": "timed_out",
                "kind": "general",
                "pr_number": 42,
                "head_sha": "operator-resolution-head",
                "failure_run_ids": [12345],
                "run_attempts": {"12345": 1},
                "prior_budget_present": False,
                "prior_budget_value": 0,
                "prior_poll": {
                    "ci_last_polled_present": True,
                    "ci_last_polled": "2026-01-01T00:00:00",
                    "ci_poll_interval_present": True,
                    "ci_poll_interval": 120,
                },
                "deadline_at": "2000-01-01T00:00:00",
            },
        },
    )
    establish_independent_approval_hold(
        task,
        reason="ci_rerun_outcome_unconfirmed",
        owner="ci_completion",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_operator_rejected_resolution_releases_exact_hold(
    client, db_session, mocker
):
    task = await _make_timed_out_task(db_session)
    invalidate = mocker.patch("app.github.invalidate_check_runs_cache")

    response = await client.post(
        f"/api/admin/tasks/{task.id}/ci-rerun-resolution",
        json={
            "journal_id": "operator-journal",
            "disposition": "rejected",
            "note": "GitHub run history proves the request was not accepted.",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "rejected"
    await db_session.refresh(task)
    assert task.requires_approval is False
    assert task.context["ci_rerun_journal"]["state"] == "rejected"
    assert task.context["ci_rerun_journal"]["operator_resolution"] == {
        "disposition": "rejected",
        "resolved_at": task.context["ci_rerun_journal"]["updated_at"],
        "resolved_by": TEST_USER_ID,
        "note": "GitHub run history proves the request was not accepted.",
    }
    assert "ci_rerun_consumed" not in task.context
    assert task.context["review_loop"]["ci_last_polled"] is None
    assert task.context["review_loop"]["ci_poll_interval"] == 15
    assert "_approval_hold_provenance" not in task.context
    invalidate.assert_called_once_with(
        "owner", "repo", "operator-resolution-head"
    )
    history = (
        await db_session.execute(
            select(TaskHistory).where(TaskHistory.task_id == task.id)
        )
    ).scalars().all()
    assert any(
        row.details.get("changes", {}).get("ci_rerun_resolution") == "rejected"
        for row in history
    )


@pytest.mark.asyncio
async def test_operator_accepted_resolution_renews_bounded_fence(
    client, db_session
):
    task = await _make_timed_out_task(db_session)

    response = await client.post(
        f"/api/admin/tasks/{task.id}/ci-rerun-resolution",
        json={
            "journal_id": "operator-journal",
            "disposition": "accepted",
            "note": "GitHub run history shows the rerun request was accepted.",
        },
    )

    assert response.status_code == 200, response.text
    await db_session.refresh(task)
    journal = task.context["ci_rerun_journal"]
    assert task.requires_approval is False
    assert journal["state"] == "accepted"
    assert task.context["ci_rerun_consumed"] == 1
    resolved_at = datetime.fromisoformat(journal["operator_resolution"]["resolved_at"])
    deadline_at = datetime.fromisoformat(journal["deadline_at"])
    assert resolved_at < deadline_at <= resolved_at + timedelta(seconds=90)
    assert task.context["review_loop"]["ci_last_polled"] is None
    assert task.context["review_loop"]["ci_poll_interval"] == 15


@pytest.mark.asyncio
async def test_operator_resolution_requires_exact_journal_and_member_role(
    client, db_session
):
    task = await _make_timed_out_task(db_session, role="viewer")
    body = {
        "journal_id": "operator-journal",
        "disposition": "rejected",
        "note": "Verified against GitHub run history.",
    }

    forbidden = await client.post(
        f"/api/admin/tasks/{task.id}/ci-rerun-resolution", json=body
    )
    assert forbidden.status_code == 403

    membership = (
        await db_session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == task.project_id,
                ProjectMember.user_id == TEST_USER_ID,
            )
        )
    ).scalar_one()
    membership.role = "member"
    db_session.add(membership)
    await db_session.commit()

    wrong_journal = await client.post(
        f"/api/admin/tasks/{task.id}/ci-rerun-resolution",
        json={**body, "journal_id": "other-journal"},
    )
    assert wrong_journal.status_code == 409
    await db_session.refresh(task)
    assert task.requires_approval is True
    assert task.context["ci_rerun_journal"]["state"] == "timed_out"

    oversized = await client.post(
        f"/api/admin/tasks/{task.id}/ci-rerun-resolution",
        json={**body, "note": "x" * 501},
    )
    assert oversized.status_code == 422
