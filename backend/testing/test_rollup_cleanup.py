"""
Tests for _cleanup_branches safety guards in rollup_management.py.

Covers:
  - Non-parent task detection (no plan_output, non-research/plan type) → no staging deletion
  - Guard 1: active children (QUEUED/PROVISIONING/IN_PROGRESS) → skip staging deletion
  - Guard 2: another EVALUATING task references same staging branch → skip
  - Guard 3: open PRs targeting the staging branch → skip
  - Guard 3: GitHub API error → skip (safe default)
  - Happy path: parent with no active children, no competing rollups, no open PRs → staging deleted
  - plan_output presence marks non-research task as parent
  - Feature branch cleanup: pr_merged=True with blueprint/ branch → feature branch deleted
  - Non-blueprint branch not deleted even with pr_merged=True
  - No staging branch in context → no deletion
"""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.rollup_management import _cleanup_branches
from app.models import Project, Task, TaskStatus


# ── Fixture ───────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture()
async def cleanup_db():
    """Isolated in-memory SQLite DB for _cleanup_branches tests."""
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(type_, **kw)

    _engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    _Session = sessionmaker(
        bind=_engine, class_=AsyncSession,
        autocommit=False, autoflush=False, expire_on_commit=False,
    )

    async with _Session() as session:
        yield session

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await _engine.dispose()


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _make_project(session):
    project = Project(name="Test Project", user_id="user1")
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def _make_task(
    session,
    project_id,
    task_type="create",
    status=TaskStatus.COMPLETED,
    parent_task_id=None,
    context=None,
    plan_output=None,
    payloads=None,
):
    if payloads is None:
        payloads = [{"type": task_type, "prompt": "test task"}]
    task = Task(
        project_id=project_id,
        parent_task_id=parent_task_id,
        payloads=payloads,
        status=status,
        context=context or {},
        plan_output=plan_output,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cleanup_non_parent_skips_staging_branch(cleanup_db, mocker):
    """A task that is NOT a parent (no plan_output, payload type not research/plan)
    should not trigger staging branch deletion even if context has plan_branch."""
    project = await _make_project(cleanup_db)
    task = await _make_task(
        cleanup_db, project.id,
        task_type="create",
        context={"plan_branch": "plan/some-branch"},
    )

    mock_delete = mocker.patch("app.github.delete_branch", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )

    await _cleanup_branches(task, cleanup_db)

    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_guard1_in_progress_child_skips_staging(cleanup_db, mocker):
    """Guard 1: an IN_PROGRESS child blocks staging branch deletion."""
    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db, project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/my-branch"},
    )
    await _make_task(
        cleanup_db, project.id,
        task_type="create",
        status=TaskStatus.IN_PROGRESS,
        parent_task_id=parent.id,
    )

    mock_delete = mocker.patch("app.github.delete_branch", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )

    await _cleanup_branches(parent, cleanup_db)

    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_guard1_queued_child_skips_staging(cleanup_db, mocker):
    """Guard 1: a QUEUED child also blocks staging branch deletion."""
    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db, project.id,
        task_type="plan",
        status=TaskStatus.EVALUATING,
        context={"plan_branch": "plan/test-branch"},
    )
    await _make_task(
        cleanup_db, project.id,
        task_type="create",
        status=TaskStatus.QUEUED,
        parent_task_id=parent.id,
    )

    mock_delete = mocker.patch("app.github.delete_branch", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )

    await _cleanup_branches(parent, cleanup_db)

    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_guard1_provisioning_child_skips_staging(cleanup_db, mocker):
    """Guard 1: a PROVISIONING child also blocks staging branch deletion."""
    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db, project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/prov-branch"},
    )
    await _make_task(
        cleanup_db, project.id,
        task_type="create",
        status=TaskStatus.PROVISIONING,
        parent_task_id=parent.id,
    )

    mock_delete = mocker.patch("app.github.delete_branch", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )

    await _cleanup_branches(parent, cleanup_db)

    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_guard2_parent_base_reference_skips(cleanup_db, mocker):
    """Guard 2: another EVALUATING task references same staging branch via parent_base → skip."""
    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db, project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/shared-branch"},
    )
    # All children completed — Guard 1 will pass
    await _make_task(
        cleanup_db, project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
    )
    # Another EVALUATING rollup references this branch as parent_base
    await _make_task(
        cleanup_db, project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"parent_base": "research/shared-branch"},
    )

    mock_delete = mocker.patch("app.github.delete_branch", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch("app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[])

    await _cleanup_branches(parent, cleanup_db)

    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_guard2_original_base_reference_skips(cleanup_db, mocker):
    """Guard 2: another EVALUATING task references same staging branch via original_base → skip."""
    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db, project.id,
        task_type="plan",
        status=TaskStatus.EVALUATING,
        context={"plan_branch": "plan/shared-base"},
    )
    await _make_task(
        cleanup_db, project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
    )
    # Another EVALUATING rollup references via original_base
    await _make_task(
        cleanup_db, project.id,
        task_type="plan",
        status=TaskStatus.EVALUATING,
        context={"original_base": "plan/shared-base"},
    )

    mock_delete = mocker.patch("app.github.delete_branch", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch("app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[])

    await _cleanup_branches(parent, cleanup_db)

    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_guard3_open_prs_skips_staging(cleanup_db, mocker):
    """Guard 3: open PRs targeting the staging branch → skip deletion."""
    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db, project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/pr-branch"},
    )
    await _make_task(
        cleanup_db, project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
    )

    mock_delete = mocker.patch("app.github.delete_branch", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_open_pull_requests",
        new_callable=AsyncMock,
        return_value=[{"number": 42, "title": "Open PR"}],
    )

    await _cleanup_branches(parent, cleanup_db)

    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_guard3_github_error_skips(cleanup_db, mocker):
    """Guard 3: if get_open_pull_requests raises an exception, skip deletion (safe default)."""
    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db, project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/error-branch"},
    )
    await _make_task(
        cleanup_db, project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
    )

    mock_delete = mocker.patch("app.github.delete_branch", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_open_pull_requests",
        new_callable=AsyncMock,
        side_effect=Exception("GitHub API unavailable"),
    )

    await _cleanup_branches(parent, cleanup_db)

    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_happy_path_research_branch_deleted(cleanup_db, mocker):
    """Happy path: research parent with all children completed, no competing rollups,
    no open PRs → research_branch is deleted."""
    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db, project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/clean-branch"},
    )
    await _make_task(
        cleanup_db, project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
    )

    mock_delete = mocker.patch("app.github.delete_branch", new_callable=AsyncMock, return_value=True)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch("app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[])

    await _cleanup_branches(parent, cleanup_db)

    mock_delete.assert_called_once_with("gh-token", "owner", "repo", "research/clean-branch")


@pytest.mark.asyncio
async def test_cleanup_happy_path_plan_branch_deleted(cleanup_db, mocker):
    """Happy path: plan parent with plan_branch context → plan_branch is deleted."""
    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db, project.id,
        task_type="plan",
        status=TaskStatus.EVALUATING,
        context={"plan_branch": "plan/finished-branch"},
    )
    # No children

    mock_delete = mocker.patch("app.github.delete_branch", new_callable=AsyncMock, return_value=True)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch("app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[])

    await _cleanup_branches(parent, cleanup_db)

    mock_delete.assert_called_once_with("gh-token", "owner", "repo", "plan/finished-branch")


@pytest.mark.asyncio
async def test_cleanup_plan_output_marks_task_as_parent(cleanup_db, mocker):
    """A task with plan_output set is identified as a parent even if payload type
    is not research/plan (e.g., type=create with plan_output from a completed plan stage)."""
    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db, project.id,
        task_type="create",
        status=TaskStatus.EVALUATING,
        context={"plan_branch": "plan/output-branch"},
        plan_output={"steps": ["implement feature"]},
    )
    # No children

    mock_delete = mocker.patch("app.github.delete_branch", new_callable=AsyncMock, return_value=True)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch("app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[])

    await _cleanup_branches(parent, cleanup_db)

    mock_delete.assert_called_once_with("gh-token", "owner", "repo", "plan/output-branch")


@pytest.mark.asyncio
async def test_cleanup_feature_branch_deleted_for_merged_pr(cleanup_db, mocker):
    """Child task with pr_merged=True in context and a blueprint/ branch in payloads
    → the feature branch is deleted."""
    project = await _make_project(cleanup_db)
    child = await _make_task(
        cleanup_db, project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create", "prompt": "create task", "branch": "blueprint/my-feature"}],
    )

    mock_delete = mocker.patch("app.github.delete_branch", new_callable=AsyncMock, return_value=True)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )

    await _cleanup_branches(child, cleanup_db)

    mock_delete.assert_called_once_with("gh-token", "owner", "repo", "blueprint/my-feature")


@pytest.mark.asyncio
async def test_cleanup_non_blueprint_branch_not_deleted(cleanup_db, mocker):
    """Feature branch that doesn't start with blueprint/ is NOT deleted
    even when pr_merged=True in context."""
    project = await _make_project(cleanup_db)
    child = await _make_task(
        cleanup_db, project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[{"type": "create", "prompt": "create task", "branch": "feature/other-branch"}],
    )

    mock_delete = mocker.patch("app.github.delete_branch", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )

    await _cleanup_branches(child, cleanup_db)

    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_no_staging_branch_in_context(cleanup_db, mocker):
    """Parent task with neither plan_branch nor research_branch in context → no staging deletion."""
    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db, project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={},  # No staging branch keys
    )

    mock_delete = mocker.patch("app.github.delete_branch", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )

    await _cleanup_branches(parent, cleanup_db)

    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_completed_children_do_not_block_staging(cleanup_db, mocker):
    """Children in terminal states (COMPLETED/FAILED/CANCELLED) do NOT block staging deletion."""
    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db, project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/done-branch"},
    )
    # All children in terminal states
    await _make_task(
        cleanup_db, project.id, task_type="create",
        status=TaskStatus.COMPLETED, parent_task_id=parent.id,
    )
    await _make_task(
        cleanup_db, project.id, task_type="create",
        status=TaskStatus.FAILED, parent_task_id=parent.id,
    )
    await _make_task(
        cleanup_db, project.id, task_type="create",
        status=TaskStatus.CANCELLED, parent_task_id=parent.id,
    )

    mock_delete = mocker.patch("app.github.delete_branch", new_callable=AsyncMock, return_value=True)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch("app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[])

    await _cleanup_branches(parent, cleanup_db)

    mock_delete.assert_called_once_with("gh-token", "owner", "repo", "research/done-branch")
