"""Focused immutable-priority contract tests for operator task creation."""

import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.admin_task_admission import (
    TaskCreationAuthority,
    build_task_creation_authority_snapshot,
    canonical_task_creation_request,
    reject_idempotent_task_authority_mismatch,
    task_creation_authority_receipt,
)
from app.admin_task_contracts import CreateTaskPayload
from app.models import Task


_CODE_EVIDENCE = {
    "kind": "observable_behavior",
    "description": "operator-spawned task ships a real, testable change",
}


def _payload(project_id: uuid.UUID, *, priority: int = 50) -> CreateTaskPayload:
    return CreateTaskPayload(
        github_repo="acme/task-priority",
        project_id=project_id,
        title="Existing exact task",
        prompt="Create this exact task.",
        task_type="create",
        base_branch="main",
        priority=priority,
        acceptance_evidence=_CODE_EVIDENCE,
    )


def _task(
    project_id: uuid.UUID, *, priority: int = 50, context: dict | None = None
) -> Task:
    return Task(
        project_id=project_id,
        title="Existing exact task",
        priority=priority,
        payloads=[
            {
                "type": "create",
                "prompt": "Create this exact task.",
                "base_branch": "main",
                "acceptance_evidence": _CODE_EVIDENCE,
            }
        ],
        context=context,
    )


@pytest.mark.parametrize("priority", [-1, 101])
def test_create_task_payload_rejects_out_of_range_priority(priority: int):
    with pytest.raises(ValidationError):
        _payload(uuid.uuid4(), priority=priority)


def test_legacy_idempotency_replay_rejects_priority_mismatch():
    project_id = uuid.uuid4()
    existing = _task(project_id)
    replay = _payload(project_id, priority=100)

    assert task_creation_authority_receipt(existing)["priority"] == 50
    with pytest.raises(HTTPException) as mismatch:
        reject_idempotent_task_authority_mismatch(
            existing,
            canonical_request=canonical_task_creation_request(
                replay, resolved_base_branch="main"
            ),
        )

    assert mismatch.value.status_code == 409
    assert "priority" in mismatch.value.detail


def test_pre_priority_fingerprint_replay_recovers_row_authority():
    project_id = uuid.uuid4()
    payload = _payload(project_id)
    canonical = canonical_task_creation_request(payload, resolved_base_branch="main")
    pre_priority_canonical = {
        key: value for key, value in canonical.items() if key != "priority"
    }
    authority = TaskCreationAuthority(
        direction_id=None,
        criterion_ids=[],
        criterion_release_version_id=None,
        depends_on=[],
    )
    existing = _task(
        project_id,
        context={
            "_operator_task_creation_authority": build_task_creation_authority_snapshot(
                authority,
                canonical_request=pre_priority_canonical,
                criterion_ids_explicit=False,
                require_deps_landed_on=None,
                release_base_branch=None,
            )
        },
    )

    reject_idempotent_task_authority_mismatch(existing, canonical_request=canonical)

    with pytest.raises(HTTPException) as mismatch:
        reject_idempotent_task_authority_mismatch(
            existing,
            canonical_request={**canonical, "priority": 100},
        )
    assert mismatch.value.status_code == 409
    assert "priority" in mismatch.value.detail
