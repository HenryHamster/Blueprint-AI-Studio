"""Tests for auto_review.py guard rails and task spawning."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.auto_review import _handle_auto_review_inner
from app.models import Project, Task, TaskStatus
from app.time_utils import _utcnow


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


async def _create_project(session: AsyncSession, **overrides) -> Project:
    defaults = {
        "name": "Test Repo",
        "user_id": "test-user-123",
        "github_owner": "acme",
        "github_repo": "webapp",
    }
    defaults.update(overrides)
    project = Project(**defaults)
    session.add(project)
    await session.flush()
    return project


def _review_target(project_id: uuid.UUID, pr_number: int) -> dict[str, str | int]:
    return {
        "project_id": str(project_id),
        "pr_number": pr_number,
        "base_sha": f"base-{pr_number}",
        "head_sha": f"head-{pr_number}",
    }


@pytest.fixture(autouse=True)
def _mock_live_review_target(mocker):
    """Keep guard-rail tests behind the real exact-target admission boundary."""
    mocker.patch(
        "app.branch_cleanup._get_project_github_token",
        new_callable=mocker.AsyncMock,
        return_value="test-token",
    )

    async def _open_pr(_token, _owner, _repo, pr_number, **_kwargs):
        return {
            "state": "open",
            "merged": False,
            "base": {"sha": f"base-{pr_number}"},
            "head": {"sha": f"head-{pr_number}"},
        }

    mocker.patch(
        "app.github.get_pull_request",
        new_callable=mocker.AsyncMock,
        side_effect=_open_pr,
    )


async def _create_auto_review_task(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    pr_number: int = 42,
    status: TaskStatus = TaskStatus.IN_PROGRESS,
    created_at_offset: timedelta | None = None,
) -> Task:
    task = Task(
        project_id=project_id,
        title=f"Review PR #{pr_number}: some title",
        payloads=[
            {
                "type": "review",
                "pr_number": pr_number,
                "branch": "feature/x",
                "base_branch": "main",
            }
        ],
        context={
            "auto_review": True,
            "pr_number": pr_number,
            "review_loop": {
                "review_target": _review_target(project_id, pr_number),
                "target_assigned_by_backend": True,
            },
        },
        status=status,
        created_by="SYSTEM_AUTO_REVIEW",
    )
    if created_at_offset is not None:
        task.created_at = _utcnow() + created_at_offset
    session.add(task)
    await session.flush()
    return task


def _default_config(**overrides) -> dict:
    cfg = {"max_per_hour": 5, "max_concurrent": 3}
    cfg.update(overrides)
    return cfg


async def _count_auto_review_tasks(session: AsyncSession, project_id: uuid.UUID) -> int:
    tasks = (
        (await session.execute(select(Task).where(Task.project_id == project_id)))
        .scalars()
        .all()
    )
    return sum(1 for t in tasks if (t.context or {}).get("auto_review") is True)


async def _call_inner(session, project_id, *, pr_number=99, config=None):
    await _handle_auto_review_inner(
        owner="acme",
        repo="webapp",
        pr_number=pr_number,
        branch="feature/test",
        base_branch="main",
        pr_title="My PR title",
        project_id=project_id,
        config=config or _default_config(),
        webhook_pr={"number": pr_number, "state": "open"},
        session=session,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_spawns_task(db_session: AsyncSession):
    """All guards pass: a review task is created with correct fields."""
    project = await _create_project(db_session)

    await _call_inner(db_session, project.id, pr_number=7)

    assert await _count_auto_review_tasks(db_session, project.id) == 1

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    task = tasks[0]

    assert task.status == TaskStatus.QUEUED
    assert task.created_by == "SYSTEM_AUTO_REVIEW"
    assert task.context["auto_review"] is True
    assert task.context["pr_number"] == 7
    assert task.first_payload["type"] == "review"
    assert task.first_payload["pr_number"] == 7
    # Title truncation: "Review PR #7: " + pr_title[:80]
    assert "Review PR #7:" in task.title
    assert "My PR title" in task.title


@pytest.mark.asyncio
async def test_title_truncation(db_session: AsyncSession):
    """Long PR titles are truncated at 80 chars."""
    project = await _create_project(db_session)
    long_title = "A" * 100

    await _handle_auto_review_inner(
        owner="acme",
        repo="webapp",
        pr_number=1,
        branch="feature/long",
        base_branch="main",
        pr_title=long_title,
        project_id=project.id,
        config=_default_config(),
        webhook_pr={"number": 1, "state": "open"},
        session=db_session,
    )

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    task = tasks[0]
    # pr_title[:80] is used, so full 100-char title should not appear
    assert long_title not in task.title
    assert long_title[:80] in task.title


# ─────────────────────────────────────────────────────────────────────────────
# Guard: rate limit
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_blocks_at_max(db_session: AsyncSession):
    """When recent auto_review count >= max_per_hour, spawn is blocked."""
    project = await _create_project(db_session)

    # Create max_per_hour=2 recent auto_review tasks (any status)
    await _create_auto_review_task(
        db_session, project.id, pr_number=1, status=TaskStatus.COMPLETED
    )
    await _create_auto_review_task(
        db_session, project.id, pr_number=2, status=TaskStatus.COMPLETED
    )
    await db_session.flush()

    await _call_inner(
        db_session, project.id, pr_number=3, config=_default_config(max_per_hour=2)
    )

    assert await _count_auto_review_tasks(db_session, project.id) == 2


@pytest.mark.asyncio
async def test_rate_limit_old_tasks_dont_count(db_session: AsyncSession):
    """Tasks older than 1 hour don't count toward the rate limit."""
    project = await _create_project(db_session)

    # 2 tasks created more than 1 hour ago
    await _create_auto_review_task(
        db_session,
        project.id,
        pr_number=1,
        status=TaskStatus.COMPLETED,
        created_at_offset=timedelta(hours=-2),
    )
    await _create_auto_review_task(
        db_session,
        project.id,
        pr_number=2,
        status=TaskStatus.COMPLETED,
        created_at_offset=timedelta(hours=-2),
    )
    await db_session.flush()

    # With max_per_hour=2, old tasks shouldn't count — spawn should succeed
    await _call_inner(
        db_session, project.id, pr_number=3, config=_default_config(max_per_hour=2)
    )

    assert await _count_auto_review_tasks(db_session, project.id) == 3


@pytest.mark.asyncio
async def test_rate_limit_non_auto_review_tasks_dont_count(db_session: AsyncSession):
    """Non-auto_review tasks in the last hour don't count toward the rate limit."""
    project = await _create_project(db_session)

    # Regular (non-auto_review) task
    regular_task = Task(
        project_id=project.id,
        title="Manual review",
        payloads=[
            {"type": "review", "pr_number": 10, "branch": "x", "base_branch": "main"}
        ],
        context={"pr_number": 10},
        status=TaskStatus.COMPLETED,
        created_by="user",
    )
    db_session.add(regular_task)
    await db_session.flush()

    # With max_per_hour=1, regular task should not block spawn
    await _call_inner(
        db_session, project.id, pr_number=11, config=_default_config(max_per_hour=1)
    )

    assert await _count_auto_review_tasks(db_session, project.id) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Guard: duplicate detection
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_active_pr_blocked(db_session: AsyncSession):
    """Active auto_review task for same PR number blocks a new spawn."""
    project = await _create_project(db_session)

    await _create_auto_review_task(
        db_session, project.id, pr_number=42, status=TaskStatus.IN_PROGRESS
    )
    await db_session.flush()

    await _call_inner(db_session, project.id, pr_number=42)

    assert await _count_auto_review_tasks(db_session, project.id) == 1


@pytest.mark.asyncio
async def test_completed_task_same_pr_not_blocked(db_session: AsyncSession):
    """COMPLETED/FAILED tasks for same PR don't block new spawns."""
    project = await _create_project(db_session)

    await _create_auto_review_task(
        db_session, project.id, pr_number=42, status=TaskStatus.COMPLETED
    )
    await _create_auto_review_task(
        db_session, project.id, pr_number=42, status=TaskStatus.FAILED
    )
    await db_session.flush()

    await _call_inner(
        db_session, project.id, pr_number=42, config=_default_config(max_per_hour=10)
    )

    assert await _count_auto_review_tasks(db_session, project.id) == 3


@pytest.mark.asyncio
async def test_active_task_different_pr_not_blocked(db_session: AsyncSession):
    """Active auto_review task for a different PR doesn't block spawn."""
    project = await _create_project(db_session)

    await _create_auto_review_task(
        db_session, project.id, pr_number=1, status=TaskStatus.IN_PROGRESS
    )
    await db_session.flush()

    await _call_inner(db_session, project.id, pr_number=2)

    assert await _count_auto_review_tasks(db_session, project.id) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Guard: concurrent limit
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_limit_blocks_at_max(db_session: AsyncSession):
    """When active auto_review count >= max_concurrent, spawn is blocked."""
    project = await _create_project(db_session)

    await _create_auto_review_task(
        db_session, project.id, pr_number=1, status=TaskStatus.IN_PROGRESS
    )
    await _create_auto_review_task(
        db_session, project.id, pr_number=2, status=TaskStatus.QUEUED
    )
    await db_session.flush()

    await _call_inner(
        db_session, project.id, pr_number=3, config=_default_config(max_concurrent=2)
    )

    assert await _count_auto_review_tasks(db_session, project.id) == 2


@pytest.mark.asyncio
async def test_concurrent_limit_boundary_allows_at_max_minus_one(
    db_session: AsyncSession,
):
    """One below max_concurrent allows a new spawn."""
    project = await _create_project(db_session)

    await _create_auto_review_task(
        db_session, project.id, pr_number=1, status=TaskStatus.IN_PROGRESS
    )
    await db_session.flush()

    # max_concurrent=2, currently 1 active — should allow spawn
    await _call_inner(
        db_session, project.id, pr_number=2, config=_default_config(max_concurrent=2)
    )

    assert await _count_auto_review_tasks(db_session, project.id) == 2


@pytest.mark.asyncio
async def test_concurrent_limit_counts_all_active_pr_numbers(db_session: AsyncSession):
    """Concurrent limit counts all active auto_review tasks regardless of PR number."""
    project = await _create_project(db_session)

    # 3 active tasks for 3 different PRs
    for pr in [10, 20, 30]:
        await _create_auto_review_task(
            db_session, project.id, pr_number=pr, status=TaskStatus.IN_PROGRESS
        )
    await db_session.flush()

    await _call_inner(
        db_session, project.id, pr_number=99, config=_default_config(max_concurrent=3)
    )

    assert await _count_auto_review_tasks(db_session, project.id) == 3
