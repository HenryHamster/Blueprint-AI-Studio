"""Regression tests for exact criterion authority across task spawn paths."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from app import acceptance_criteria as ac
from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
from app.models import Direction, Project, ReleaseVersion, Task, TaskStatus


async def _make_project(db_session, name: str) -> Project:
    project = Project(name=name, user_id="exact-criteria-test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _make_version(db_session, project: Project) -> ReleaseVersion:
    version = ReleaseVersion(project_id=project.id, version_number=1)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return version


@pytest.mark.asyncio
async def test_explicit_task_criterion_ids_are_not_widened_from_direction(db_session):
    """An operator-authored subset proves only that subset at merge time."""
    project = await _make_project(db_session, "ExactOperatorCriterionSubset")
    version = await _make_version(db_session, project)
    selected = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Selected operator criterion",
        created_by="operator",
    )
    unrelated = await ac.create_criterion(
        db_session,
        project_id=project.id,
        release_version_id=version.id,
        text="Unrelated direction criterion",
        created_by="operator",
    )
    db_session.add(
        Direction(
            project_id=project.id,
            direction_key="d-broad",
            data={
                "id": "d-broad",
                "status": "active",
                "criterion_release_version_id": str(version.id),
            },
            status="active",
            criterion_keys=[selected.criterion_key, unrelated.criterion_key],
        )
    )
    task = Task(
        project_id=project.id,
        title="Exact subset",
        payloads=[{"type": "create", "prompt": "prove one criterion"}],
        context={
            "direction_id": "d-broad",
            "criterion_ids_explicit": True,
            "criterion_release_version_id": str(version.id),
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "satisfied": True,
                "proven": True,
                "kind": "named_test",
                "reason": "the declared test passed",
            },
        },
        criterion_ids=[selected.criterion_key],
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()

    recorded = await ac.record_task_merge_evidence(db_session, task, project.id)

    assert recorded == 2
    assert await ac.get_evidence_for_criterion(db_session, selected.id)
    assert await ac.get_evidence_for_criterion(db_session, unrelated.id) == []


@pytest.mark.asyncio
async def test_plan_completion_preserves_explicit_criterion_subset(db_session, mocker):
    from app.plan_completion import _handle_plan_completion

    project = await _make_project(db_session, "PlanExactCriterionPropagation")
    version = await _make_version(db_session, project)
    selected = await ac.create_criterion(
        db_session, project.id, version.id, "Selected plan-child criterion"
    )
    unrelated = await ac.create_criterion(
        db_session, project.id, version.id, "Unrelated direction criterion"
    )
    db_session.add(
        Direction(
            project_id=project.id,
            direction_key="d-plan-exact",
            data={
                "id": "d-plan-exact",
                "text": "Plan exact propagation",
                "status": "active",
                "criterion_release_version_id": str(version.id),
            },
            status="active",
            criterion_keys=[selected.criterion_key, unrelated.criterion_key],
        )
    )
    plan_task = Task(
        project_id=project.id,
        title="Plan: preserve exact criteria",
        payloads=[{"type": "plan", "prompt": "split work", "base_branch": "main"}],
        context={
            "direction_id": "d-plan-exact",
            "criterion_ids_explicit": True,
            "criterion_release_version_id": str(version.id),
        },
        criterion_ids=[selected.criterion_key],
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add(plan_task)
    await db_session.commit()
    await db_session.refresh(plan_task)
    mocker.patch(
        "app.task_completion._ensure_branch",
        new_callable=AsyncMock,
        return_value=plan_task,
    )
    mocker.patch("app.plan_completion.broadcast_to_project", new_callable=AsyncMock)

    await _handle_plan_completion(
        plan_task,
        json.dumps(
            {
                "subtasks": [
                    {
                        "title": "feat: exact implementation",
                        "prompt": "do only the selected work",
                        "scope": "small",
                    }
                ]
            }
        ),
        db_session,
    )

    child = (
        await db_session.execute(
            select(Task).where(Task.parent_task_id == plan_task.id)
        )
    ).scalar_one()
    assert child.criterion_ids == [selected.criterion_key]
    assert unrelated.criterion_key not in child.criterion_ids
    assert child.context["criterion_ids_explicit"] is True
    assert child.context["criterion_release_version_id"] == str(version.id)


@pytest.mark.asyncio
async def test_research_completion_preserves_explicit_criterion_subset(
    db_session, mocker
):
    from app.research_completion import _handle_research_completion
    from test_autonomous_research import _patch_research_externals

    project = await _make_project(db_session, "Research-Exact-Criteria")
    version = await _make_version(db_session, project)
    selected = await ac.create_criterion(
        db_session, project.id, version.id, "Selected research child criterion"
    )
    unrelated = await ac.create_criterion(
        db_session, project.id, version.id, "Unrelated research direction criterion"
    )
    direction_id = "d-research-exact"
    other_direction_id = "d-research-other"
    project.directions = [
        {"id": direction_id, "text": "Exact research", "status": "active"},
        {"id": other_direction_id, "text": "Other research", "status": "active"},
    ]
    db_session.add(
        Direction(
            project_id=project.id,
            direction_key=direction_id,
            data={
                "id": direction_id,
                "text": "Exact research",
                "status": "active",
                "criterion_release_version_id": str(version.id),
            },
            status="active",
            criterion_keys=[selected.criterion_key, unrelated.criterion_key],
        )
    )
    parent = Task(
        project_id=project.id,
        title="Research exact subset",
        payloads=[{"type": "research", "base_branch": "main"}],
        context={
            "direction_id": direction_id,
            "research_depth": 0,
            "criterion_ids_explicit": True,
            "criterion_release_version_id": str(version.id),
        },
        criterion_ids=[selected.criterion_key],
        status=TaskStatus.COMPLETED,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
    )
    db_session.add_all([project, parent])
    await db_session.commit()
    await db_session.refresh(parent)
    finding = {
        "title": "Implement selected criterion",
        "description": "Implement only the selected bounded behavior",
        "prompt": "Implement only the selected bounded behavior",
        "priority": "medium",
        "scope": "small",
        "risk": "low",
        "type": "improvement",
        "direction_id": other_direction_id,
        "acceptance_evidence": {
            "kind": "named_test",
            "test_path": "backend/testing/test_explicit_criterion_propagation.py",
        },
    }
    _patch_research_externals(mocker)
    mocker.patch(
        "app.research_completion._parse_research_findings", return_value=[finding]
    )
    mocker.patch(
        "app.research_completion._assess_project_spawn_health",
        return_value=(False, None),
    )

    await _handle_research_completion(parent, "", db_session)

    child = (
        await db_session.execute(
            select(Task).where(Task.parent_task_id == parent.id)
        )
    ).scalar_one()
    assert child.criterion_ids == [selected.criterion_key]
    assert unrelated.criterion_key not in child.criterion_ids
    assert child.context["direction_id"] == direction_id
    assert child.context["direction_id"] != other_direction_id
    assert child.context["criterion_ids_explicit"] is True
    assert child.context["criterion_release_version_id"] == str(version.id)
