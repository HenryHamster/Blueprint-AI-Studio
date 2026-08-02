"""Atomic ownership revalidation for post-commit recovery claims."""

import uuid

import pytest

from app import services as _services  # noqa: F401 - import-order barrier
from app.models import Project, Task, TaskStatus
from app.recovery_post_commit_claim import _claim_and_bump_total_attempts


async def _pending_task(db_session) -> tuple[Task, dict]:
    project = Project(
        name=f"Post-commit claim {uuid.uuid4()}",
        user_id="test-user",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    meta = {
        "at": "2026-08-01T00:00:00+00:00",
        "generation_token": uuid.uuid4().hex,
        "attempts": 1,
        "total_attempts": 3,
        "payload": {"task_id": "placeholder", "status": "COMPLETED"},
    }
    task = Task(
        project_id=project.id,
        title="Pending recovery claim",
        payloads=[{"type": "review", "pr_number": 5301}],
        status=TaskStatus.COMPLETED,
        post_commit_pending=True,
        context={"_post_commit_pending_meta": meta},
    )
    db_session.add(task)
    await db_session.commit()
    return task, meta


@pytest.mark.asyncio
async def test_recovery_claim_bumps_only_matching_owned_row(db_session):
    task, stale_meta = await _pending_task(db_session)

    claimed = await _claim_and_bump_total_attempts(
        task.id,
        stale_meta,
        expected_status=TaskStatus.COMPLETED,
        expected_generation_token=stale_meta["generation_token"],
    )

    assert claimed is not None
    assert claimed["total_attempts"] == 4
    assert claimed["generation_token"] == stale_meta["generation_token"]
    assert claimed["payload"] == stale_meta["payload"]
    assert claimed["claim_token"]
    await db_session.refresh(task)
    on_disk = (task.context or {})["_post_commit_pending_meta"]
    assert on_disk["total_attempts"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["marker_cleared", "archived", "status_changed"])
async def test_recovery_claim_defers_when_ownership_changes_under_lock(
    db_session, race
):
    task, stale_meta = await _pending_task(db_session)
    if race == "marker_cleared":
        task.post_commit_pending = False
    elif race == "archived":
        task.archived = True
    else:
        task.status = TaskStatus.QUEUED
    db_session.add(task)
    await db_session.commit()

    claimed = await _claim_and_bump_total_attempts(
        task.id,
        stale_meta,
        expected_status=TaskStatus.COMPLETED,
        expected_generation_token=stale_meta["generation_token"],
    )

    assert claimed is None
    await db_session.refresh(task)
    on_disk = (task.context or {})["_post_commit_pending_meta"]
    assert on_disk["total_attempts"] == 3


@pytest.mark.asyncio
async def test_recovery_claim_rejects_same_status_pending_aba_generation(db_session):
    task, stale_meta = await _pending_task(db_session)
    newer_generation = uuid.uuid4().hex
    newer_payload = {
        "task_id": str(task.id),
        "status": TaskStatus.COMPLETED.value,
        "logs": "new generation",
    }
    task.context = {
        "_post_commit_pending_meta": {
            **stale_meta,
            "generation_token": newer_generation,
            "payload": newer_payload,
        }
    }
    db_session.add(task)
    await db_session.commit()

    claimed = await _claim_and_bump_total_attempts(
        task.id,
        stale_meta,
        expected_status=TaskStatus.COMPLETED,
        expected_generation_token=stale_meta["generation_token"],
    )

    assert claimed is None
    await db_session.refresh(task)
    current = (task.context or {})["_post_commit_pending_meta"]
    assert current["generation_token"] == newer_generation
    assert current["payload"] == newer_payload
    assert "claim_token" not in current
