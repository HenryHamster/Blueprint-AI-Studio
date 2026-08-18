"""Regression coverage for BLOCKED-task rechecks under a held autonomy flag."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.blocked_task_unblock_sweeps import sweep_unblock_blocked_tasks
from app.models import Project, Task, TaskStatus
from app.time_utils import _utcnow


@pytest.mark.asyncio
async def test_due_file_conflict_requeues_with_autonomous_spawning_disabled(
    db_session, mocker
):
    """Disabling new autonomous roots must not strand an existing task graph."""

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    now = _utcnow()
    conflicting_file = "agent/src/prompts/pr-helper.ts"
    project = Project(
        name="blocked-recheck-autonomy",
        user_id="blocked-recheck-user",
        autonomous_enabled=False,
        paused=False,
        archived=False,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    completed_writer = Task(
        project_id=project.id,
        title="completed conflicting writer",
        description="The writer already landed before the blocked recheck became due.",
        status=TaskStatus.COMPLETED,
        created_by="test-user",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={"related_files": [conflicting_file]},
        status_log=[],
    )
    blocked = Task(
        project_id=project.id,
        title="blocked sibling",
        description="Must resume after the completed writer releases the file.",
        status=TaskStatus.BLOCKED,
        created_by="test-user",
        requires_approval=False,
        payloads=[{"type": "refactor"}],
        context={
            "related_files": [conflicting_file],
            "_precondition_defer": {
                "key": f"file conflict on {conflicting_file}",
                "count": 3,
                "files": [conflicting_file],
            },
        },
        status_log=[],
        blocker_kind="file_conflict",
        blocked_reason=(
            f"Waiting: file conflict on {conflicting_file} "
            "(blocked by: completed conflicting writer)"
        ),
        blocked_at=now - timedelta(minutes=40),
        recheck_after=now - timedelta(minutes=30),
    )
    db_session.add_all([completed_writer, blocked])
    await db_session.commit()

    requeued = await sweep_unblock_blocked_tasks(db_session, now)

    assert requeued == 1
    await db_session.refresh(blocked)
    assert blocked.status == TaskStatus.QUEUED
    assert blocked.blocker_kind is None
    assert blocked.recheck_after is None
    assert blocked.attempt_history[-1]["status"] == "BLOCKED"
