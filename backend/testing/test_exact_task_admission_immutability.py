"""Regression tests for immutable exact operator task admission."""

import pytest
from httpx import AsyncClient

from app.models import Task
from testing.test_admin_create_task import (
    _CODE_EVIDENCE,
    _make_project_with_member,
)


@pytest.mark.asyncio
async def test_exact_admission_rejects_payload_and_dependency_retargeting(
    client: AsyncClient, db_session
):
    """A member PATCH cannot make a creation receipt attest different work."""
    project = await _make_project_with_member(db_session, "acme/exact-patch-fence")
    project.integration_branch = "automain"
    original_dep = Task(
        project_id=project.id,
        title="Original prerequisite",
        payloads=[
            {"type": "create", "prompt": "original", "base_branch": "automain"}
        ],
    )
    replacement_dep = Task(
        project_id=project.id,
        title="Replacement prerequisite",
        payloads=[
            {"type": "create", "prompt": "replacement", "base_branch": "automain"}
        ],
    )
    db_session.add_all([project, original_dep, replacement_dep])
    await db_session.commit()

    body = {
        "github_repo": "acme/exact-patch-fence",
        "project_id": str(project.id),
        "title": "Exact admitted work",
        "prompt": "Execute the exact operator prompt.",
        "task_type": "create",
        "base_branch": "automain",
        "idempotency_key": "exact-patch-fence-v1",
        "depends_on": [str(original_dep.id)],
        "require_deps_landed_on": "automain",
        "release_base_branch": "automain",
        "acceptance_evidence": _CODE_EVIDENCE,
    }
    created_response = await client.post("/api/admin/tasks", json=body)
    assert created_response.status_code == 200, created_response.text
    task_id = created_response.json()["id"]
    created = await db_session.get(Task, task_id)
    assert created is not None
    original_payloads = [dict(item) for item in created.payloads]

    retarget_payload = [
        {
            **original_payloads[0],
            "prompt": "Execute different work.",
            "model": "different-model",
            "effort": "low",
            "hotfix": True,
            "work_class": "different-class",
            "require_deps_landed_on": None,
            "release_base_branch": "main",
        }
    ]
    payload_patch = await client.patch(
        f"/api/admin/tasks/{task_id}", json={"payloads": retarget_payload}
    )
    assert payload_patch.status_code == 400, payload_patch.text
    assert "exact operator admission authority: payloads" in payload_patch.text

    dependency_patch = await client.patch(
        f"/api/admin/tasks/{task_id}",
        json={"depends_on": [str(replacement_dep.id)]},
    )
    assert dependency_patch.status_code == 400, dependency_patch.text
    assert "exact operator admission authority: depends_on" in dependency_patch.text

    await db_session.refresh(created)
    assert created.payloads == original_payloads
    assert created.depends_on == [str(original_dep.id)]

    replay = await client.post("/api/admin/tasks", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["is_duplicate"] is True
    assert replay.json()["id"] == task_id
    assert replay.json()["depends_on"] == [str(original_dep.id)]
