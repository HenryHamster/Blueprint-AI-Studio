import uuid

import pytest
from httpx import AsyncClient

from app.models import Artifact, ArtifactType, TaskStatus, _utcnow
from testing.factories import make_membership, make_project, make_task


async def _project_with_owner(db_session):
    project = make_project(name="Cancel Impact")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))
    await db_session.commit()
    return project


@pytest.mark.asyncio
async def test_cancel_impact_counts_full_scope_and_vm_pr_signals(
    client: AsyncClient, db_session
):
    project = await _project_with_owner(db_session)
    other_project = make_project(name="Other Project", user_id="other-user")
    db_session.add(other_project)
    await db_session.commit()
    await db_session.refresh(other_project)

    target = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        vm_instance_id="agent-vm-assigned-before-provisioning",
        vm_provisioned_at=None,
        context={"review_loop": {"pr_number": 123}},
    )
    db_session.add(target)
    await db_session.commit()
    await db_session.refresh(target)

    now = _utcnow()
    active_children = [
        make_task(
            project_id=project.id,
            parent_task_id=target.id,
            status=TaskStatus.QUEUED,
        ),
        make_task(
            project_id=project.id,
            parent_task_id=target.id,
            status=TaskStatus.PROVISIONING,
            vm_instance_id="agent-vm-child-provisioning",
        ),
        make_task(
            project_id=project.id,
            parent_task_id=target.id,
            status=TaskStatus.IN_PROGRESS,
            vm_instance_id="agent-vm-child-running",
            vm_provisioned_at=now,
            context={"rollup_pr_number": 456},
        ),
        make_task(
            project_id=project.id,
            parent_task_id=target.id,
            status=TaskStatus.EVALUATING,
        ),
    ]
    non_cascade_children = [
        make_task(
            project_id=project.id,
            parent_task_id=target.id,
            status=TaskStatus.BLOCKED,
        ),
        make_task(
            project_id=project.id,
            parent_task_id=target.id,
            status=TaskStatus.COMPLETED,
        ),
    ]

    counted_dependents = [
        make_task(
            project_id=project.id,
            status=TaskStatus.QUEUED,
            depends_on=[str(target.id)],
        ),
        make_task(
            project_id=project.id,
            status=TaskStatus.BLOCKED,
            depends_on=[str(target.id)],
        ),
        make_task(
            project_id=project.id,
            status=TaskStatus.PROVISIONING,
            depends_on=[str(target.id)],
        ),
        make_task(
            project_id=project.id,
            status=TaskStatus.PARKED,
            depends_on=[str(target.id)],
        ),
    ]
    ignored_dependents = [
        make_task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            depends_on=[str(target.id)],
        ),
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            depends_on=[str(target.id)],
        ),
        make_task(
            project_id=project.id,
            status=TaskStatus.QUEUED,
            depends_on=[str(uuid.uuid4())],
        ),
        make_task(
            project_id=other_project.id,
            status=TaskStatus.QUEUED,
            depends_on=[str(target.id)],
        ),
    ]
    db_session.add_all(
        active_children
        + non_cascade_children
        + counted_dependents
        + ignored_dependents
    )
    await db_session.commit()

    db_session.add_all(
        [
            Artifact(
                task_id=target.id,
                artifact_type=ArtifactType.PULL_REQUEST,
                pr_url="https://github.com/acme/repo/pull/123",
            ),
            Artifact(
                task_id=active_children[2].id,
                artifact_type=ArtifactType.PULL_REQUEST,
                pr_url="https://github.com/acme/repo/pull/456",
            ),
            Artifact(
                task_id=counted_dependents[0].id,
                artifact_type=ArtifactType.PULL_REQUEST,
                pr_url="https://github.com/acme/repo/pull/789",
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get(f"/api/admin/tasks/{target.id}/cancel-impact")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["task_id"] == str(target.id)
    assert data["status"] == "QUEUED"
    assert data["active_child_cascade_count"] == 4
    assert data["active_child_cascade_statuses"] == [
        "QUEUED",
        "PROVISIONING",
        "IN_PROGRESS",
        "EVALUATING",
    ]
    assert data["waiting_dependent_count"] == 4
    assert data["waiting_dependent_statuses"] == [
        "QUEUED",
        "BLOCKED",
        "PROVISIONING",
        "PARKED",
    ]
    assert data["target_vm_instance_id"] == "agent-vm-assigned-before-provisioning"
    assert data["target_has_vm_id"] is True
    assert data["target_vm_provisioned"] is False
    assert data["active_child_vm_id_count"] == 2
    assert data["active_child_vm_provisioned_count"] == 1
    assert data["orphan_pr_cleanup_context_reference_count"] == 2
    assert data["pull_request_artifact_task_count"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("task_id", ["not-a-uuid", str(uuid.uuid4())])
async def test_cancel_impact_returns_404_for_missing_task(
    client: AsyncClient, task_id: str
):
    resp = await client.get(f"/api/admin/tasks/{task_id}/cancel-impact")
    assert resp.status_code == 404
