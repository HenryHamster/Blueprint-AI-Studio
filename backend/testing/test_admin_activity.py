"""Tests for /api/admin/activity PR impact annotations on TASK_COMPLETED events."""

from datetime import timedelta

import pytest
from httpx import AsyncClient

from sqlalchemy import text

from app.models import (
    Project,
    ProjectMember,
    Task,
    TaskStatus,
    Artifact,
    ArtifactType,
    _utcnow,
)


TEST_USER_ID = "test-user-123"


async def _make_project(session, name="Activity Test Project"):
    project = Project(name=name, user_id=TEST_USER_ID)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(
        ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner")
    )
    await session.commit()
    return project


@pytest.mark.asyncio
async def test_completed_task_includes_pr_impact_annotations(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    many_files = [f"backend/app/mod_{i}.py" for i in range(12)]
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        title="Feat: do the thing",
        context={
            "_pr_changed_files": many_files,
            "scope": "backend",
            "direction_id": "dir-abc-123-xyz",
            "review_loop": {"iteration": 3},
        },
    )
    db_session.add(task)
    await db_session.commit()

    resp = await client.get("/api/admin/activity")
    assert resp.status_code == 200
    events = resp.json()["events"]
    completed = [e for e in events if e["type"] == "TASK_COMPLETED"]
    assert len(completed) == 1
    ev = completed[0]
    assert ev["changed_files"] is not None
    assert len(ev["changed_files"]) == 10
    assert ev["changed_files"] == many_files[:10]
    assert ev["scope"] == "backend"
    assert ev["direction_id"] == "dir-abc-123-xyz"
    assert ev["review_iterations"] == 3


@pytest.mark.asyncio
async def test_completed_task_missing_context_fields_are_none(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        title="Bare completed task",
        context={},
    )
    db_session.add(task)
    await db_session.commit()

    resp = await client.get("/api/admin/activity")
    assert resp.status_code == 200
    completed = [e for e in resp.json()["events"] if e["type"] == "TASK_COMPLETED"]
    assert len(completed) == 1
    ev = completed[0]
    assert ev["changed_files"] is None
    assert ev["scope"] is None
    assert ev["direction_id"] is None
    assert ev["review_iterations"] is None


@pytest.mark.parametrize(
    ("task_status", "event_type"),
    [
        (TaskStatus.COMPLETED, "TASK_COMPLETED"),
        (TaskStatus.FAILED, "TASK_FAILED"),
    ],
)
@pytest.mark.asyncio
async def test_terminal_task_created_before_window_uses_completed_at(
    task_status: TaskStatus,
    event_type: str,
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    now = _utcnow()
    completed_at = now - timedelta(minutes=5)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "long running"}],
        status=task_status,
        title="Long running task",
        created_at=now - timedelta(days=14),
        started_at=now - timedelta(days=14),
        completed_at=completed_at,
        status_log=[
            {
                "s": task_status.value,
                "t": completed_at.isoformat(),
            }
        ],
    )
    db_session.add(task)
    await db_session.commit()

    resp = await client.get("/api/admin/activity")

    assert resp.status_code == 200
    terminal_events = [e for e in resp.json()["events"] if e["type"] == event_type]
    assert [event["task_id"] for event in terminal_events] == [str(task.id)]
    assert terminal_events[0]["timestamp"] == completed_at.isoformat()


@pytest.mark.asyncio
async def test_terminal_task_without_completed_at_uses_updated_at_not_status_log(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    now = _utcnow()
    updated_at = now - timedelta(minutes=3)
    old_terminal_at = now - timedelta(days=10)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "legacy terminal row"}],
        status=TaskStatus.COMPLETED,
        title="Legacy terminal task",
        created_at=now - timedelta(days=14),
        started_at=now - timedelta(days=14),
        completed_at=None,
        updated_at=updated_at,
        status_log=[
            {
                "s": TaskStatus.COMPLETED.value,
                "t": old_terminal_at.isoformat(),
            }
        ],
    )
    task.updated_at = updated_at
    db_session.add(task)
    await db_session.commit()

    # The before_insert event listener (_stamp_task_completed_at_on_insert) stamps
    # completed_at from the status_log tail (old_terminal_at = 10 days ago), which
    # would push the task outside the 7-day activity window. Use a raw SQL UPDATE
    # to simulate a legacy row that predates the durable-column stamping, bypassing
    # all ORM events (onupdate, before_flush) so updated_at stays at the fixture value.
    await db_session.execute(
        text(
            "UPDATE task SET completed_at = NULL, updated_at = :ua WHERE id = :id"
        ),
        {"ua": updated_at, "id": task.id},
    )
    await db_session.commit()

    resp = await client.get("/api/admin/activity")

    assert resp.status_code == 200
    completed = [e for e in resp.json()["events"] if e["type"] == "TASK_COMPLETED"]
    assert [event["task_id"] for event in completed] == [str(task.id)]
    assert completed[0]["timestamp"] == updated_at.isoformat()


@pytest.mark.asyncio
async def test_failed_task_includes_failure_class_and_taxonomy(
    client: AsyncClient, db_session
):
    project = await _make_project(db_session)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.FAILED,
        title="Failed task",
        latest_status_message="AGENT_TIMEOUT: timed out",
        context={"failure_class": "AGENT_TIMEOUT"},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.ERROR_LOG,
        logs="agent timed out after 3600s",
    )
    db_session.add(artifact)
    await db_session.commit()

    resp = await client.get("/api/admin/activity")
    assert resp.status_code == 200
    events = resp.json()["events"]
    failed = [e for e in events if e["type"] == "TASK_FAILED"]
    assert len(failed) == 1
    ev = failed[0]
    assert ev["failure_class"] is not None
    assert ev["taxonomy"] is not None
    assert ev["error_summary"] is not None


@pytest.mark.asyncio
async def test_completed_event_task_type_uses_projected_first_payload(
    client: AsyncClient, db_session
):
    """task_type is read via the SQL-projected payloads[0], not the full array."""
    project = await _make_project(db_session, name="Activity First-Payload Project")
    task = Task(
        project_id=project.id,
        # Second element carries a different type; only the first must be used.
        payloads=[{"type": "refactor", "prompt": "x"}, {"type": "create"}],
        status=TaskStatus.COMPLETED,
        title="Refactor task",
        context={},
    )
    db_session.add(task)
    await db_session.commit()

    resp = await client.get("/api/admin/activity")
    assert resp.status_code == 200
    completed = [e for e in resp.json()["events"] if e["type"] == "TASK_COMPLETED"]
    assert len(completed) == 1
    assert completed[0]["task_type"] == "refactor"


@pytest.mark.asyncio
async def test_research_finding_count_from_narrow_plan_output_fetch(
    client: AsyncClient, db_session
):
    """RESEARCH_FINDINGS finding_count is computed from the narrowly-fetched
    plan_output (which is no longer loaded for every terminal feed row)."""
    project = await _make_project(db_session, name="Activity Research Project")
    task = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        title="Research task",
        context={},
        plan_output=[{"finding": "a"}, {"finding": "b"}, {"finding": "c"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    db_session.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.RESEARCH_FINDINGS,
            logs="[]",
        )
    )
    await db_session.commit()

    resp = await client.get("/api/admin/activity")
    assert resp.status_code == 200
    research = [e for e in resp.json()["events"] if e["type"] == "RESEARCH_FINDINGS"]
    assert len(research) == 1
    assert research[0]["task_id"] == str(task.id)
    assert research[0]["finding_count"] == 3


@pytest.mark.asyncio
async def test_activity_includes_pr_artifact_when_multiple_artifacts_present(
    client: AsyncClient, db_session
):
    """Artifact query returns data correctly when a task has multiple artifacts."""
    project = await _make_project(db_session, name="Activity Multi-Artifact Project")
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "x"}],
        status=TaskStatus.COMPLETED,
        title="Multi-artifact task",
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Add both an ERROR_LOG and a PULL_REQUEST artifact for the same task
    db_session.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.ERROR_LOG,
            logs="some earlier error",
        )
    )
    db_session.add(
        Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/example/repo/pull/42",
            repo_url="https://github.com/example/repo",
        )
    )
    await db_session.commit()

    resp = await client.get("/api/admin/activity")
    assert resp.status_code == 200
    events = resp.json()["events"]
    completed = [e for e in events if e["type"] == "TASK_COMPLETED"]
    assert len(completed) == 1
    ev = completed[0]
    # PR artifact should be picked up despite the presence of other artifact types
    assert ev["pr_url"] == "https://github.com/example/repo/pull/42"
