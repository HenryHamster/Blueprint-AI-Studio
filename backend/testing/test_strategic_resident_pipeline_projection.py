"""Resident generations remain auditable without masquerading as pipeline work."""

import pytest
from sqlalchemy import func
from sqlmodel import select

from app import strategic_cycle
from app.database import resolve_dialect_name
from app.models import (
    Project,
    ProjectMember,
    StrategicCycle,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.strategic_completion import _handle_strategic_completion
from app.strategic_state_read_model import _build_task_focus
from app.time_utils import _utcnow


TEST_USER_ID = "test-user-123"


async def _project_with_member(db_session):
    project = Project(name="Resident pipeline projection", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        ProjectMember(
            project_id=project.id,
            user_id=TEST_USER_ID,
            role="owner",
        )
    )
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.mark.asyncio
async def test_four_resident_generations_project_as_one_real_pipeline_work_item(
    client,
    db_session,
):
    project = await _project_with_member(db_session)
    resident_specs = (
        (TaskStatus.IN_PROGRESS, {"_resident_session": True}),
        (
            TaskStatus.EVALUATING,
            {"_resident_session": True, "_resident_infrastructure": True},
        ),
        (
            TaskStatus.FAILED,
            {
                "_resident_session": True,
                "_resident_infrastructure": True,
                "_resident_lifecycle_disposition": "worker_superseded",
            },
        ),
        (
            TaskStatus.COMPLETED,
            {
                "_resident_session": True,
                "_resident_infrastructure": True,
                "_resident_lifecycle_disposition": "operator_stop",
            },
        ),
    )
    residents = [
        Task(
            project_id=project.id,
            title=f"Resident generation {index}",
            payloads=[{"type": "strategic_review"}],
            status=status,
            context=context,
            latest_status_message=f"Resident generation {index} lifecycle ended",
        )
        for index, (status, context) in enumerate(resident_specs, start=1)
    ]
    implementation = Task(
        project_id=project.id,
        title="Implement the actual project change",
        payloads=[{"type": "create"}],
        status=TaskStatus.IN_PROGRESS,
        context={},
        latest_status_message="Implementing verified work",
    )
    db_session.add_all([*residents, implementation])
    await db_session.flush()
    db_session.add(
        TaskHistory(
            task_id=residents[0].id,
            action="RESIDENT_GENERATION_ENDED",
            details={"reason": "worker_exit"},
        )
    )
    await db_session.commit()

    ordinary = await client.get(f"/api/admin/tasks?project_id={project.id}")
    assert ordinary.status_code == 200, ordinary.text
    ordinary_body = ordinary.json()
    assert ordinary_body["total"] == 1
    assert [row["id"] for row in ordinary_body["tasks"]] == [str(implementation.id)]
    assert ordinary_body["active_count"] == 1
    assert ordinary_body["scoped_active_count"] == 1
    assert ordinary_body["evaluating_count"] == 0

    counts = await client.get(f"/api/admin/tasks/counts?project_id={project.id}")
    assert counts.status_code == 200, counts.text
    assert counts.json()["scoped_active_count"] == 1
    assert counts.json()["scoped_evaluating_count"] == 0

    operator_tasks = await client.get(
        f"/api/agent-access/v1/tasks?project_id={project.id}"
    )
    assert operator_tasks.status_code == 200, operator_tasks.text
    assert operator_tasks.json()["total"] == 1
    assert [row["id"] for row in operator_tasks.json()["tasks"]] == [
        str(implementation.id)
    ]

    # Explicit audit mode retains every raw generation, including the legacy
    # row that predates the dedicated infrastructure marker.
    audit = await client.get(
        f"/api/admin/tasks?project_id={project.id}&include_resident_infrastructure=true"
    )
    assert audit.status_code == 200, audit.text
    audit_body = audit.json()
    assert audit_body["total"] == 5
    audit_residents = [
        row for row in audit_body["tasks"] if row["context"].get("_resident_session")
    ]
    assert len(audit_residents) == 4
    assert all(
        row["latest_status_message"] != "Strategic output could not be parsed"
        for row in audit_residents
    )

    project_tasks = await client.get(f"/api/projects/{project.id}/tasks")
    assert project_tasks.status_code == 200, project_tasks.text
    assert [row["id"] for row in project_tasks.json()["tasks"]] == [
        str(implementation.id)
    ]
    project_audit = await client.get(
        f"/api/projects/{project.id}/tasks?include_resident_infrastructure=true"
    )
    assert project_audit.status_code == 200, project_audit.text
    assert (
        len(
            [
                row
                for row in project_audit.json()["tasks"]
                if row["context"].get("_resident_session")
            ]
        )
        == 4
    )

    focus = await _build_task_focus(db_session, project.id)
    assert focus["buckets"]["active"]["total"] == 1
    assert focus["buckets"]["active"]["items"][0]["id"] == str(implementation.id)
    assert focus["buckets"]["friction"]["total"] == 0
    assert focus["buckets"]["recent_completed"]["total"] == 0

    # The complete strategic-state endpoint contains an unrelated PostgreSQL
    # UNION shape that SQLite cannot execute. Exercise its exact API contract
    # whenever the production dialect is available; the shared task-focus
    # projection above remains dialect-independent coverage.
    if resolve_dialect_name(db_session) == "postgresql":
        strategic_state = await client.get(
            f"/api/admin/projects/{project.id}/strategic-state"
        )
        assert strategic_state.status_code == 200, strategic_state.text
        assert strategic_state.json()["tasks"]["counts"]["total"] == 1

    projects = await client.get("/api/admin/projects?archived=false")
    assert projects.status_code == 200, projects.text
    project_row = next(
        item for item in projects.json()["items"] if item["id"] == str(project.id)
    )
    assert project_row["task_status_counts"]["total"] == 1

    attention = await client.get(
        f"/api/admin/operator-attention?project_id={project.id}"
    )
    assert attention.status_code == 200, attention.text
    attention_body = attention.json()
    assert attention_body["counts"]["failed_tasks"] == 0
    resident_ids = {str(task.id) for task in residents}
    assert not any(
        str((item.get("ref") or {}).get("id")) in resident_ids
        for item in attention_body["items"]
    )

    # Default projection did not erase provenance: task detail and its history
    # remain directly queryable, and all four raw Task rows still exist.
    detail = await client.get(f"/api/admin/tasks/{residents[0].id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["context"]["_resident_session"] is True
    assert detail.json()["history"][0]["action"] == "RESIDENT_GENERATION_ENDED"
    operator_detail = await client.get(f"/api/agent-access/v1/tasks/{residents[0].id}")
    assert operator_detail.status_code == 200, operator_detail.text
    assert operator_detail.json()["task"]["id"] == str(residents[0].id)
    assert operator_detail.json()["history"][0]["action"] == (
        "RESIDENT_GENERATION_ENDED"
    )
    raw_resident_count = await db_session.scalar(
        select(func.count())
        .select_from(Task)
        .where(
            Task.project_id == project.id,
            Task.context["_resident_session"].as_boolean().is_(True),
        )
    )
    assert raw_resident_count == 4


@pytest.mark.asyncio
async def test_resident_terminal_without_result_skips_generic_strategic_failure_path(
    db_session,
    monkeypatch,
):
    project = await _project_with_member(db_session)
    task = Task(
        project_id=project.id,
        title="Resident generation terminal",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.COMPLETED,
        context={"_resident_session": True},
        latest_status_message="Resident process generation ended",
    )
    db_session.add(task)
    await db_session.flush()
    cycle = StrategicCycle(
        id="sc-resident-generation-terminal",
        project_id=project.id,
        outcome=strategic_cycle.OUTCOME_PHASE2_RUNNING,
        phase1_started_at=_utcnow(),
        phase1_completed_at=_utcnow(),
        phase2_started_at=_utcnow(),
        phase2_task_id=task.id,
    )
    task.strategic_cycle_id = cycle.id
    db_session.add_all([task, cycle])
    await db_session.commit()

    emitted_events = []

    async def _capture_terminal_events(events):
        emitted_events.extend(events)

    monkeypatch.setattr(
        strategic_cycle,
        "emit_terminal_completion_events",
        _capture_terminal_events,
    )
    await _handle_strategic_completion(task, "", db_session, project=project)
    # Completion sweeps are retryable; the infrastructure disposition and
    # operator-useful lifecycle status remain stable on a duplicate invocation,
    # and its adopted cadence cycle closes exactly once.
    await _handle_strategic_completion(task, "", db_session, project=project)

    await db_session.refresh(task)
    await db_session.refresh(project)
    await db_session.refresh(cycle)
    assert task.status == TaskStatus.COMPLETED
    assert task.context["_resident_infrastructure"] is True
    assert (
        task.context["_resident_lifecycle_disposition"]
        == "resident_generation_terminal"
    )
    assert task.latest_status_message == "Resident process generation ended"
    assert cycle.outcome == strategic_cycle.OUTCOME_FAILED_PHASE2
    assert (
        cycle.cycle_metadata[strategic_cycle.CYCLE_META_TERMINAL_DISPOSITION]
        == strategic_cycle.TERMINAL_DISPOSITION_CANCELLED
    )
    assert cycle.failure_reason == (
        "terminal_task_reconciliation: source=resident_generation_terminal "
        f"task_id={task.id} status=COMPLETED"
    )
    assert len(emitted_events) == 1
    assert emitted_events[0]["cycle_id"] == cycle.id
    assert "completion" not in emitted_events[0]
    assert "_strategic_failures" not in (project.vision or {})
    assert "_strategic_failures_at" not in (project.vision or {})


@pytest.mark.asyncio
async def test_strategic_state_separates_starting_running_and_evaluating(
    client,
    db_session,
):
    project = await _project_with_member(db_session)
    tasks = [
        Task(
            project_id=project.id,
            title=f"Lifecycle {status.value}",
            payloads=[{"type": "create"}],
            status=status,
            context={},
        )
        for status in (
            TaskStatus.QUEUED,
            TaskStatus.PROVISIONING,
            TaskStatus.IN_PROGRESS,
            TaskStatus.EVALUATING,
        )
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/projects/{project.id}/strategic-state"
    )

    assert response.status_code == 200, response.text
    counts = response.json()["tasks"]["counts"]
    assert counts["queued"] == 1
    assert counts["starting"] == 1
    assert counts["running"] == 1
    assert counts["evaluating"] == 1
    assert counts["total"] == 4
