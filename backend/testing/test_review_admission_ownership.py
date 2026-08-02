"""Focused admission regressions for durable review/fix ownership."""

import uuid
from unittest.mock import AsyncMock

import pytest

from app.fix_admission import (
    FixAdmissionDenial,
    prepare_direct_fix_admission,
)
from app.models import Project, Task, TaskStatus
from app.review_admission import prepare_direct_review_admission


async def _project(db_session) -> Project:
    project = Project(
        name=f"Admission ownership {uuid.uuid4()}",
        user_id="test-user",
        github_owner="owner",
        github_repo="repo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


def _target(project: Project, pr_number: int) -> dict[str, str | int]:
    return {
        "project_id": str(project.id),
        "pr_number": pr_number,
        "base_sha": "base-a",
        "head_sha": "head-a",
    }


def _open_pr() -> dict:
    return {
        "state": "open",
        "merged": False,
        "base": {"sha": "base-a"},
        "head": {"sha": "head-a"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("quota"), None])
async def test_fix_admission_classifies_unproven_live_state_retryable(
    db_session, failure
):
    project = await _project(db_session)
    if failure is not None:
        fetch_live = AsyncMock(side_effect=failure)
    else:
        fetch_live = AsyncMock(
            return_value={"state": "open", "base": {}, "head": {}}
        )

    decision = await prepare_direct_fix_admission(
        db_session,
        project_id=project.id,
        pr_number=5101,
        fix_task_id=uuid.uuid4(),
        fetch_live_pr=fetch_live,
    )

    assert decision.allowed is False
    assert decision.denial is FixAdmissionDenial.RETRYABLE


@pytest.mark.asyncio
@pytest.mark.parametrize("live_pr", [{}, {"state": "unknown", "merged": False}])
async def test_fix_admission_classifies_unknown_live_state_retryable(
    db_session, live_pr
):
    project = await _project(db_session)

    decision = await prepare_direct_fix_admission(
        db_session,
        project_id=project.id,
        pr_number=5106,
        fix_task_id=uuid.uuid4(),
        fetch_live_pr=AsyncMock(return_value=live_pr),
    )

    assert decision.allowed is False
    assert decision.denial is FixAdmissionDenial.RETRYABLE


@pytest.mark.asyncio
async def test_fix_admission_classifies_closed_pr_terminal(db_session):
    project = await _project(db_session)

    decision = await prepare_direct_fix_admission(
        db_session,
        project_id=project.id,
        pr_number=5102,
        fix_task_id=uuid.uuid4(),
        fetch_live_pr=AsyncMock(return_value={"state": "closed", "merged": False}),
    )

    assert decision.allowed is False
    assert decision.denial is FixAdmissionDenial.TERMINAL


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "pending"),
    [
        (TaskStatus.BLOCKED, False),
        (TaskStatus.PARKED, False),
        (TaskStatus.COMPLETED, True),
        (TaskStatus.FAILED, True),
    ],
)
async def test_fix_admission_classifies_durable_owner_occupied(
    db_session, status, pending
):
    project = await _project(db_session)
    pr_number = 5103
    owner = Task(
        project_id=project.id,
        title="Existing fix owner",
        payloads=[{"type": "fix", "pr_number": pr_number}],
        context={"review_loop": {"fix_target": _target(project, pr_number)}},
        status=status,
        post_commit_pending=pending,
    )
    db_session.add(owner)
    await db_session.commit()

    decision = await prepare_direct_fix_admission(
        db_session,
        project_id=project.id,
        pr_number=pr_number,
        fix_task_id=uuid.uuid4(),
        fetch_live_pr=AsyncMock(return_value=_open_pr()),
    )

    assert decision.allowed is False
    assert decision.denial is FixAdmissionDenial.OCCUPIED
    assert str(owner.id) in decision.reason


@pytest.mark.asyncio
async def test_fix_readmission_cannot_exclude_itself_while_pending(db_session):
    project = await _project(db_session)
    pr_number = 5104
    owner = Task(
        project_id=project.id,
        title="Pending self",
        payloads=[{"type": "fix", "pr_number": pr_number}],
        context={"review_loop": {"fix_target": _target(project, pr_number)}},
        status=TaskStatus.COMPLETED,
        post_commit_pending=True,
    )
    db_session.add(owner)
    await db_session.commit()

    decision = await prepare_direct_fix_admission(
        db_session,
        project_id=project.id,
        pr_number=pr_number,
        fix_task_id=owner.id,
        fetch_live_pr=AsyncMock(return_value=_open_pr()),
    )

    assert decision.denial is FixAdmissionDenial.OCCUPIED


@pytest.mark.asyncio
async def test_review_admission_reuses_terminal_pending_owner(db_session):
    project = await _project(db_session)
    pr_number = 5105
    owner = Task(
        project_id=project.id,
        title="Pending exact review",
        payloads=[{"type": "review", "pr_number": pr_number}],
        context={"review_loop": {"review_target": _target(project, pr_number)}},
        status=TaskStatus.COMPLETED,
        post_commit_pending=True,
    )
    db_session.add(owner)
    await db_session.commit()

    decision = await prepare_direct_review_admission(
        db_session,
        project_id=project.id,
        pr_number=pr_number,
        fetch_live_pr=AsyncMock(return_value=_open_pr()),
    )

    assert decision.allowed is False
    assert decision.existing_task is owner
    assert "pending post-commit continuation" in decision.reason


@pytest.mark.asyncio
async def test_fix_admission_treats_live_open_legacy_terminal_marker_as_retryable(
    db_session,
):
    project = await _project(db_session)
    pr_number = 5107
    legacy = Task(
        project_id=project.id,
        title="Contradictory legacy terminal state",
        payloads=[{"type": "review", "pr_number": pr_number}],
        context={"pr_closed_unmerged": True},
        status=TaskStatus.COMPLETED,
    )
    db_session.add(legacy)
    await db_session.commit()

    decision = await prepare_direct_fix_admission(
        db_session,
        project_id=project.id,
        pr_number=pr_number,
        fix_task_id=uuid.uuid4(),
        fetch_live_pr=AsyncMock(return_value=_open_pr()),
    )

    assert decision.allowed is False
    assert decision.denial is FixAdmissionDenial.RETRYABLE


@pytest.mark.asyncio
async def test_fix_admission_pending_stale_review_blocks_new_fix(db_session):
    project = await _project(db_session)
    pr_number = 5108
    owner = Task(
        project_id=project.id,
        title="Pending stale review",
        payloads=[{"type": "review", "pr_number": pr_number}],
        context={
            "review_loop": {
                "review_target": {
                    **_target(project, pr_number),
                    "head_sha": "old-head",
                }
            }
        },
        status=TaskStatus.COMPLETED,
        post_commit_pending=True,
    )
    db_session.add(owner)
    await db_session.commit()

    decision = await prepare_direct_fix_admission(
        db_session,
        project_id=project.id,
        pr_number=pr_number,
        fix_task_id=uuid.uuid4(),
        fetch_live_pr=AsyncMock(return_value=_open_pr()),
    )

    assert decision.allowed is False
    assert decision.denial is FixAdmissionDenial.OCCUPIED
    assert "same-PR review" in decision.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_type", ["fix", "review"])
async def test_review_admission_pending_nonexact_owner_blocks_without_reuse(
    db_session, owner_type
):
    project = await _project(db_session)
    pr_number = 5109
    target_key = "fix_target" if owner_type == "fix" else "review_target"
    owner = Task(
        project_id=project.id,
        title=f"Pending nonexact {owner_type}",
        payloads=[{"type": owner_type, "pr_number": pr_number}],
        context={
            "review_loop": {
                target_key: {
                    **_target(project, pr_number),
                    "head_sha": "old-head",
                }
            }
        },
        status=TaskStatus.FAILED,
        post_commit_pending=True,
    )
    db_session.add(owner)
    await db_session.commit()

    decision = await prepare_direct_review_admission(
        db_session,
        project_id=project.id,
        pr_number=pr_number,
        fetch_live_pr=AsyncMock(return_value=_open_pr()),
    )

    assert decision.allowed is False
    assert decision.existing_task is None
    assert f"same-PR {owner_type}" in decision.reason
    assert "pending post-commit continuation" in decision.reason
