"""Execution-boundary regressions for queued/revived PR work."""

import uuid
from unittest.mock import AsyncMock

import pytest

from app.models import Project, Task, TaskStatus
from app.review_claim_fence import lock_pr_task_for_execution


def _target(project: Project, pr_number: int, *, head: str = "head-a") -> dict:
    return {
        "project_id": str(project.id),
        "pr_number": pr_number,
        "base_sha": "base-a",
        "head_sha": head,
    }


def _mock_live_pr(mocker, *, head: str = "head-a") -> AsyncMock:
    mocker.patch(
        "app.review_claim_fence.get_github_token_for_task",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    return mocker.patch(
        "app.review_claim_fence.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "base": {"sha": "base-a"},
                "head": {"sha": head},
            }
        ),
    )


@pytest.mark.asyncio
async def test_claim_fence_collapses_duplicate_exact_review(db_session, mocker):
    project = Project(
        name="Claim fence review dedup",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    pr_number = 4101
    target = _target(project, pr_number)
    owner = Task(
        id=uuid.UUID(int=1),
        project_id=project.id,
        title="Canonical exact review",
        payloads=[{"type": "review", "pr_number": pr_number}],
        context={"review_loop": {"review_target": target}},
        status=TaskStatus.QUEUED,
    )
    duplicate = Task(
        id=uuid.UUID(int=2),
        project_id=project.id,
        title="Duplicate exact review",
        payloads=[{"type": "review", "pr_number": pr_number}],
        context={"review_loop": {"review_target": target}},
        status=TaskStatus.QUEUED,
    )
    db_session.add_all([owner, duplicate])
    await db_session.commit()
    _mock_live_pr(mocker)

    decision = await lock_pr_task_for_execution(
        db_session,
        task_id=duplicate.id,
        claimable_statuses=(TaskStatus.QUEUED,),
    )
    await db_session.commit()

    assert decision.allowed is False
    assert "already owns execution" in decision.reason
    await db_session.refresh(duplicate)
    assert duplicate.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_claim_fence_revalidates_in_progress_review_replay(
    db_session, mocker
):
    project = Project(
        name="Claim fence replay",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    pr_number = 4102
    task = Task(
        project_id=project.id,
        title="Stale replay review",
        payloads=[{"type": "review", "pr_number": pr_number}],
        context={
            "review_loop": {"review_target": _target(project, pr_number, head="old")}
        },
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    _mock_live_pr(mocker, head="new")

    decision = await lock_pr_task_for_execution(
        db_session,
        task_id=task.id,
        claimable_statuses=(TaskStatus.IN_PROGRESS,),
    )
    await db_session.commit()

    assert decision.allowed is False
    assert decision.reason == "assigned review target is stale"
    await db_session.refresh(task)
    assert task.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_claim_fence_rejects_targetless_fix(db_session, mocker):
    project = Project(
        name="Claim fence targetless fix",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        title="Legacy targetless writer",
        payloads=[{"type": "fix", "pr_number": 4103}],
        context={"review_loop": {}},
        status=TaskStatus.QUEUED,
    )
    db_session.add(task)
    await db_session.commit()
    _mock_live_pr(mocker)

    decision = await lock_pr_task_for_execution(
        db_session,
        task_id=task.id,
        claimable_statuses=(TaskStatus.QUEUED,),
    )
    await db_session.commit()

    assert decision.allowed is False
    assert decision.reason == "fix has no immutable assigned target"
    await db_session.refresh(task)
    assert task.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_targetless_legacy_fix_cannot_own_valid_fix_execution(
    db_session, mocker
):
    project = Project(
        name="Claim fence valid fix wins",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    pr_number = 4105
    target = _target(project, pr_number)
    legacy = Task(
        id=uuid.UUID(int=1),
        project_id=project.id,
        title="Older targetless writer",
        payloads=[{"type": "fix", "pr_number": pr_number}],
        context={"review_loop": {}},
        status=TaskStatus.QUEUED,
    )
    valid = Task(
        id=uuid.UUID(int=2),
        project_id=project.id,
        title="Current exact writer",
        payloads=[{"type": "fix", "pr_number": pr_number}],
        context={"review_loop": {"fix_target": target}},
        status=TaskStatus.QUEUED,
    )
    db_session.add_all([legacy, valid])
    await db_session.commit()
    _mock_live_pr(mocker)

    decision = await lock_pr_task_for_execution(
        db_session,
        task_id=valid.id,
        claimable_statuses=(TaskStatus.QUEUED,),
    )

    assert decision.allowed is True
    assert decision.task is valid


@pytest.mark.asyncio
async def test_claim_fence_revokes_review_authority_before_fix_runs(
    db_session, mocker
):
    project = Project(
        name="Claim fence fix revocation",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    pr_number = 4104
    target = _target(project, pr_number)
    source = Task(
        project_id=project.id,
        title="Previously approved source",
        payloads=[{"type": "create", "pr_number": pr_number}],
        context={
            "review_loop": {
                "review_approved": True,
                "review_not_required": True,
                "review_target": target,
            }
        },
        status=TaskStatus.EVALUATING,
    )
    fix = Task(
        project_id=project.id,
        title="Exact-target writer",
        payloads=[{"type": "fix", "pr_number": pr_number}],
        context={"review_loop": {"fix_target": target}},
        status=TaskStatus.QUEUED,
    )
    db_session.add_all([source, fix])
    await db_session.commit()
    _mock_live_pr(mocker)

    decision = await lock_pr_task_for_execution(
        db_session,
        task_id=fix.id,
        claimable_statuses=(TaskStatus.QUEUED,),
    )
    await db_session.commit()

    assert decision.allowed is True
    await db_session.refresh(source)
    loop = (source.context or {})["review_loop"]
    assert loop["review_approved"] is False
    assert loop["ci_fix_spawned"] == str(fix.id)
    assert "review_not_required" not in loop


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type", ["review", "fix"])
async def test_claim_fence_refuses_current_pending_continuation(
    db_session, mocker, task_type
):
    project = Project(
        name=f"Claim fence current pending {task_type}",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    pr_number = 4106
    target_key = "review_target" if task_type == "review" else "fix_target"
    task = Task(
        project_id=project.id,
        title="Illegally requeued pending continuation",
        payloads=[{"type": task_type, "pr_number": pr_number}],
        context={"review_loop": {target_key: _target(project, pr_number)}},
        status=TaskStatus.QUEUED,
        post_commit_pending=True,
    )
    db_session.add(task)
    await db_session.commit()
    live_mock = _mock_live_pr(mocker)

    decision = await lock_pr_task_for_execution(
        db_session,
        task_id=task.id,
        claimable_statuses=(TaskStatus.QUEUED,),
    )

    assert decision.allowed is False
    assert decision.reason == "task has a pending post-commit continuation"
    live_mock.assert_not_awaited()
    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert task.post_commit_pending is True


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type", ["review", "fix"])
@pytest.mark.parametrize(
    ("owner_status", "owner_pending"),
    [
        (TaskStatus.BLOCKED, False),
        (TaskStatus.PARKED, False),
        (TaskStatus.COMPLETED, True),
    ],
)
async def test_claim_fence_preserves_durable_owner_over_lower_id_contender(
    db_session,
    mocker,
    task_type,
    owner_status,
    owner_pending,
):
    project = Project(
        name=f"Claim fence durable owner {task_type} {owner_status}",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    pr_number = 4107
    target_key = "review_target" if task_type == "review" else "fix_target"
    target = _target(project, pr_number)
    contender = Task(
        id=uuid.UUID(int=1),
        project_id=project.id,
        title="New lower-id contender",
        payloads=[{"type": task_type, "pr_number": pr_number}],
        context={"review_loop": {target_key: target}},
        status=TaskStatus.QUEUED,
    )
    owner = Task(
        id=uuid.UUID(int=2),
        project_id=project.id,
        title="Existing durable owner",
        payloads=[{"type": task_type, "pr_number": pr_number}],
        context={"review_loop": {target_key: target}},
        status=owner_status,
        post_commit_pending=owner_pending,
    )
    db_session.add_all([contender, owner])
    await db_session.commit()
    _mock_live_pr(mocker)

    decision = await lock_pr_task_for_execution(
        db_session,
        task_id=contender.id,
        claimable_statuses=(TaskStatus.QUEUED,),
    )
    await db_session.commit()

    assert decision.allowed is False
    assert str(owner.id) in decision.reason
    await db_session.refresh(contender)
    await db_session.refresh(owner)
    assert contender.status == TaskStatus.CANCELLED
    assert owner.status == owner_status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner_type", "contender_type"),
    [
        ("review", "fix"),
        ("fix", "review"),
    ],
)
async def test_claim_fence_pending_owner_blocks_cross_type_and_stale_target(
    db_session,
    mocker,
    owner_type,
    contender_type,
):
    project = Project(
        name=f"Claim fence pending {owner_type} blocks {contender_type}",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    pr_number = 4108
    owner_target_key = (
        "review_target" if owner_type == "review" else "fix_target"
    )
    contender_target_key = (
        "review_target" if contender_type == "review" else "fix_target"
    )
    contender = Task(
        id=uuid.UUID(int=1),
        project_id=project.id,
        title="New cross-type contender",
        payloads=[{"type": contender_type, "pr_number": pr_number}],
        context={
            "review_loop": {
                contender_target_key: _target(project, pr_number, head="head-a")
            }
        },
        status=TaskStatus.QUEUED,
    )
    owner = Task(
        id=uuid.UUID(int=2),
        project_id=project.id,
        title="Pending stale-target owner",
        payloads=[{"type": owner_type, "pr_number": pr_number}],
        context={
            "review_loop": {
                owner_target_key: _target(project, pr_number, head="old-head")
            }
        },
        status=TaskStatus.COMPLETED,
        post_commit_pending=True,
    )
    db_session.add_all([contender, owner])
    await db_session.commit()
    live_mock = _mock_live_pr(mocker)

    decision = await lock_pr_task_for_execution(
        db_session,
        task_id=contender.id,
        claimable_statuses=(TaskStatus.QUEUED,),
    )
    await db_session.commit()

    assert decision.allowed is False
    assert f"same-PR {owner_type}" in decision.reason
    assert str(owner.id) in decision.reason
    live_mock.assert_not_awaited()
    await db_session.refresh(contender)
    await db_session.refresh(owner)
    assert contender.status == TaskStatus.CANCELLED
    assert owner.status == TaskStatus.COMPLETED
    assert owner.post_commit_pending is True
