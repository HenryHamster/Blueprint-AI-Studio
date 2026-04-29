"""
Tests for _cleanup_branches and _verify_pr_merged in rollup_management.py.

Covers _cleanup_branches:
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

Covers _verify_pr_merged:
  - Context cache hit (pr_merged=True already set) → returns True without API call
  - No PR artifact → caches pr_merged=True and returns True
  - GitHub API confirms merged → returns True, updates task.context
  - GitHub API 401 → invalidates token cache and retries with fresh token
  - Transient GitHub errors (500, 502, 503) → returns False without failing task
  - Closed-but-not-merged → marks task FAILED, creates alert, returns True
"""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.rollup_management import _cleanup_branches, _verify_pr_merged
from app.models import Project, Task, TaskStatus, Artifact, ArtifactType


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


# ── _verify_pr_merged helpers ─────────────────────────────────────────────────

def _make_write_session_mock(db_task=None):
    """Return a mock async context manager for AsyncSessionLocal()."""
    write_session = AsyncMock()
    write_session.get = AsyncMock(return_value=db_task)
    write_session.add = MagicMock()
    write_session.commit = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=write_session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, write_session


async def _make_artifact(session, task_id, pr_url):
    art = Artifact(
        task_id=task_id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url=pr_url,
    )
    session.add(art)
    await session.commit()
    return art


# ── _verify_pr_merged tests ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verify_pr_merged_context_cache_hit(cleanup_db, mocker):
    """If task.context already has pr_merged=True, returns True without any API call."""
    project = await _make_project(cleanup_db)
    task = await _make_task(
        cleanup_db, project.id,
        context={"pr_merged": True},
    )

    mock_get_pr = mocker.patch("app.github.get_pull_request", new_callable=AsyncMock)

    result = await _verify_pr_merged(task, cleanup_db)

    assert result is True
    mock_get_pr.assert_not_called()


@pytest.mark.asyncio
async def test_verify_pr_merged_no_artifact(cleanup_db, mocker):
    """No PR artifact → treated as done (no PR needed), pr_merged cached, returns True."""
    project = await _make_project(cleanup_db)
    task = await _make_task(cleanup_db, project.id, context={})

    mock_get_pr = mocker.patch("app.github.get_pull_request", new_callable=AsyncMock)

    result = await _verify_pr_merged(task, cleanup_db)

    assert result is True
    assert task.context.get("pr_merged") is True
    mock_get_pr.assert_not_called()


@pytest.mark.asyncio
async def test_verify_pr_merged_api_confirms_merged(cleanup_db, mocker):
    """GitHub API says merged=True → returns True, updates task.context."""
    project = await _make_project(cleanup_db)
    task = await _make_task(cleanup_db, project.id, context={})
    await _make_artifact(cleanup_db, task.id, "https://github.com/owner/repo/pull/42")

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True, "state": "closed"},
    )
    ctx, _ = _make_write_session_mock(db_task=None)
    mocker.patch("app.rollup_management.AsyncSessionLocal", return_value=ctx)

    result = await _verify_pr_merged(task, cleanup_db)

    assert result is True
    assert task.context.get("pr_merged") is True


@pytest.mark.asyncio
async def test_verify_pr_merged_401_retries_with_fresh_token(cleanup_db, mocker):
    """GitHub API 401 → invalidates token cache and retries with force_refresh=True."""
    project = await _make_project(cleanup_db)
    task = await _make_task(cleanup_db, project.id, context={})
    await _make_artifact(cleanup_db, task.id, "https://github.com/owner/repo/pull/7")

    mock_get_token = mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("fresh-token", "owner", "repo"),
    )
    mock_invalidate = mocker.patch("app.routers.agent.invalidate_github_token_cache")
    mock_get_pr = mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=[
            HTTPException(status_code=401, detail="Unauthorized"),
            {"merged": True, "state": "closed"},
        ],
    )
    ctx, _ = _make_write_session_mock(db_task=None)
    mocker.patch("app.rollup_management.AsyncSessionLocal", return_value=ctx)

    result = await _verify_pr_merged(task, cleanup_db)

    assert result is True
    mock_invalidate.assert_called_once_with(str(task.project_id))
    # Second call must use force_refresh=True
    _, kwargs = mock_get_token.call_args_list[-1]
    assert kwargs.get("force_refresh") is True
    assert mock_get_pr.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [500, 502, 503])
async def test_verify_pr_merged_transient_errors_return_false(cleanup_db, mocker, status_code):
    """Transient GitHub errors (500/502/503) → returns False without failing the task."""
    project = await _make_project(cleanup_db)
    task = await _make_task(cleanup_db, project.id, context={})
    await _make_artifact(cleanup_db, task.id, "https://github.com/owner/repo/pull/99")

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=HTTPException(status_code=status_code, detail="Error"),
    )

    result = await _verify_pr_merged(task, cleanup_db)

    assert result is False
    assert task.status != TaskStatus.FAILED


@pytest.mark.asyncio
async def test_verify_pr_merged_closed_not_merged_fails_task(cleanup_db, mocker):
    """PR closed without merging → task marked FAILED, alert created, returns True."""
    project = await _make_project(cleanup_db)
    task = await _make_task(cleanup_db, project.id, context={})
    await _make_artifact(cleanup_db, task.id, "https://github.com/owner/repo/pull/55")

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": False, "state": "closed"},
    )
    mocker.patch(
        "app.failure_classification.classify_failure",
        return_value="CODE_ERROR",
    )
    mocker.patch("app.routers.agent_status.record_task_result")
    mock_alert = mocker.patch(
        "app.main._maybe_create_failure_alert",
        new_callable=AsyncMock,
    )

    db_task_mock = MagicMock()
    db_task_mock.context = {}
    ctx, write_session = _make_write_session_mock(db_task=db_task_mock)
    write_session.get = AsyncMock(return_value=db_task_mock)
    mocker.patch("app.rollup_management.AsyncSessionLocal", return_value=ctx)

    result = await _verify_pr_merged(task, cleanup_db)

    assert result is True
    assert task.status == TaskStatus.FAILED
    assert task.context.get("pr_closed_unmerged") is True
    assert "55" in task.latest_status_message
    mock_alert.assert_called_once()


# ── Tests for persist_failure_category at rollup parent-failure paths ─────────

@pytest.mark.asyncio
async def test_all_children_failed_parent_persists_failure_category(cleanup_db, mocker):
    """When all children fail and the rollup parent marks itself FAILED,
    failure_category must be set on parent.context (path 5 — all-children-failed).

    Children's categories are aggregated via worst_child_failure_category:
    CODE_ERROR > AGENT_TIMEOUT > infra classes.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    # Prevent GitHub token fetch (not needed for this path)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)

    # Parent task with research plan_output so it's treated as a rollup parent
    parent = await _make_task(
        cleanup_db, project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={
            "_requeue_count": 3,  # exhaust retry limit
            "plan_branch": "blueprint/plan-xyz",
            "parent_base": "main",
        },
        plan_output="some plan",
    )

    # Two FAILED children — one CODE_ERROR, one AGENT_TIMEOUT
    child1 = await _make_task(
        cleanup_db, project.id,
        parent_task_id=parent.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "AGENT_TIMEOUT"},
    )
    child2 = await _make_task(
        cleanup_db, project.id,
        parent_task_id=parent.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "CODE_ERROR"},
    )

    await _check_and_trigger_rollup(child1, cleanup_db)

    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    # CODE_ERROR should win over AGENT_TIMEOUT
    assert parent.context.get("failure_category") == "CODE_ERROR"


@pytest.mark.asyncio
async def test_all_children_failed_with_only_infra_failures(cleanup_db, mocker):
    """When all failed children have infra categories, the parent keeps that category."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)

    parent = await _make_task(
        cleanup_db, project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={
            "_requeue_count": 3,
            "plan_branch": "blueprint/plan-abc",
            "parent_base": "main",
        },
        plan_output="some plan",
    )

    child = await _make_task(
        cleanup_db, project.id,
        parent_task_id=parent.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "PROVISION_FAILURE"},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    assert parent.context.get("failure_category") == "PROVISION_FAILURE"


@pytest.mark.asyncio
async def test_zero_diff_rollup_pr_closed_without_spawning_review(cleanup_db, mocker):
    """A rollup PR with 0 additions + 0 deletions is closed immediately:
    parent goes COMPLETED, staging branch deleted, no review task spawned."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    # Target branch exists, staging branch exists, no open PRs targeting staging
    mocker.patch("app.github.get_branch_sha", new_callable=AsyncMock, return_value="abc123")
    mocker.patch("app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[])
    # Rollup PR is created (not empty_diff) but its diff fetch returns 0 lines changed
    mocker.patch(
        "app.github.create_pull_request",
        new_callable=AsyncMock,
        return_value={"number": 4242, "html_url": "https://github.com/owner/repo/pull/4242", "empty_diff": False},
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"additions": 0, "deletions": 0, "merged": False, "state": "open"},
    )
    mock_close = mocker.patch("app.github.close_pull_request", new_callable=AsyncMock, return_value=True)
    mock_delete = mocker.patch("app.github.delete_branch", new_callable=AsyncMock, return_value=True)
    # Critical: verify review task is NOT spawned on zero-diff PR
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, None),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)

    parent = await _make_task(
        cleanup_db, project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={
            "research_branch": "research/zero-diff",
            "parent_base": "main",
        },
        plan_output="some plan",
    )
    # One child that is COMPLETED with pr_merged=True so rollup proceeds
    child = await _make_task(
        cleanup_db, project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED
    mock_close.assert_awaited_once()
    # close_pull_request called with rollup PR number
    close_args = mock_close.call_args
    assert close_args.args[3] == 4242 or close_args.kwargs.get("pr_number") == 4242
    # Staging branch deleted
    assert any(call.args[-1] == "research/zero-diff" for call in mock_delete.call_args_list)
    # No review/fix task spawned
    mock_spawn.assert_not_called()
    # Rollup PR marker cleared so reaper/retry logic doesn't resume with a closed PR
    assert "rollup_pr_number" not in (parent.context or {})


@pytest.mark.asyncio
async def test_unverified_pr_size_defers_instead_of_spawning_review(cleanup_db, mocker):
    """When get_pull_request fails transiently, defer the review-spawn decision
    to the next rollup cycle rather than assuming large and burning VM time on
    a PR whose size is unverified. Pre-2026-04-17 this path defaulted
    lines_changed=999 → review spawned on potentially-empty PRs → ~15
    VM-hours/day wasted per the audit."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock, return_value=("token", "owner", "repo"),
    )
    mocker.patch("app.github.get_branch_sha", new_callable=AsyncMock, return_value="abc123")
    mocker.patch("app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[])
    mocker.patch(
        "app.github.create_pull_request",
        new_callable=AsyncMock,
        return_value={"number": 7070, "html_url": "https://github.com/owner/repo/pull/7070", "empty_diff": False},
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock, side_effect=Exception("GitHub 502"),
    )
    mock_close = mocker.patch("app.github.close_pull_request", new_callable=AsyncMock)
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock, return_value=(None, None),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db, project.id, task_type="research", status=TaskStatus.EVALUATING,
        context={"research_branch": "research/unverified", "parent_base": "main"},
        plan_output="plan",
    )
    child = await _make_task(
        cleanup_db, project.id, parent_task_id=parent.id,
        status=TaskStatus.COMPLETED, context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING, (
        f"Unverified PR should defer, not transition. Got {parent.status}: {parent.latest_status_message}"
    )
    mock_close.assert_not_awaited()
    mock_spawn.assert_not_called()


@pytest.mark.asyncio
async def test_marker_commit_failure_rescues_pr_number(cleanup_db, mocker):
    """When PR creation succeeds but the marker commit fails, the exception
    handler must persist rollup_pr_number via a separate session so the
    retry cycle's crash-recovery check resumes the existing PR rather than
    creating a duplicate orphan."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch("app.github.get_branch_sha", new_callable=AsyncMock, return_value="abc123")
    mocker.patch("app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[])
    # PR creation itself succeeds — the bug is in the marker persistence that follows.
    mock_create = mocker.patch(
        "app.github.create_pull_request",
        new_callable=AsyncMock,
        return_value={
            "number": 9999,
            "html_url": "https://github.com/owner/repo/pull/9999",
            "empty_diff": False,
        },
    )

    # Rescue session: captures the marker write the exception handler makes
    # via AsyncSessionLocal() after the primary session's commit fails.
    db_parent_mock = MagicMock()
    db_parent_mock.context = {}
    rescue_session = AsyncMock()
    rescue_session.get = AsyncMock(return_value=db_parent_mock)
    rescue_session.add = MagicMock()
    rescue_session.commit = AsyncMock()
    rescue_ctx = MagicMock()
    rescue_ctx.__aenter__ = AsyncMock(return_value=rescue_session)
    rescue_ctx.__aexit__ = AsyncMock(return_value=False)
    mocker.patch("app.rollup_management.AsyncSessionLocal", return_value=rescue_ctx)

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db, project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/marker-fail", "parent_base": "main"},
        plan_output="some plan",
    )
    child = await _make_task(
        cleanup_db, project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    # Fail the marker commit inside _check_and_trigger_rollup. Phase B3
    # added two lock-window-break commits ahead of the marker commit:
    #   1. _release_parent_lock before get_open_pull_requests (read cluster)
    #   2. _release_parent_lock before create_pull_request
    #   3. The marker commit itself (the bug-target — failure here orphans the PR)
    # We let the first two commits succeed and only fail the marker so the
    # rescue path is what we observe.
    real_commit = cleanup_db.commit
    commit_calls = {"n": 0}
    MARKER_COMMIT_INDEX = 3

    async def flaky_commit(*args, **kwargs):
        commit_calls["n"] += 1
        if commit_calls["n"] == MARKER_COMMIT_INDEX:
            raise RuntimeError("connection reset during marker commit")
        return await real_commit(*args, **kwargs)

    cleanup_db.commit = flaky_commit

    await _check_and_trigger_rollup(child, cleanup_db)

    # PR was created exactly once — the rescue prevents a second orphan PR
    mock_create.assert_awaited_once()

    # The rescue path must have used AsyncSessionLocal to persist the marker
    rescue_session.get.assert_awaited_once()
    get_args = rescue_session.get.await_args.args
    assert get_args[0] is Task
    assert get_args[1] == parent.id
    rescue_session.add.assert_called_once_with(db_parent_mock)
    rescue_session.commit.assert_awaited_once()
    # Context written to rescue session contains the PR number/url so that
    # the next retry cycle's crash-recovery branch can resume this PR.
    assert db_parent_mock.context.get("rollup_pr_number") == 9999
    assert db_parent_mock.context.get("rollup_pr_url") == "https://github.com/owner/repo/pull/9999"


@pytest.mark.asyncio
async def test_marker_commit_failure_skips_rescue_when_db_already_has_marker(cleanup_db, mocker):
    """If the marker is already persisted in the DB (e.g. the commit partially
    succeeded or a prior cycle wrote it), the rescue path must NOT overwrite
    it — the existing value is authoritative."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch("app.github.get_branch_sha", new_callable=AsyncMock, return_value="abc123")
    mocker.patch("app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[])
    mocker.patch(
        "app.github.create_pull_request",
        new_callable=AsyncMock,
        return_value={
            "number": 9999,
            "html_url": "https://github.com/owner/repo/pull/9999",
            "empty_diff": False,
        },
    )

    db_parent_mock = MagicMock()
    # Simulate a stored marker from a prior cycle
    db_parent_mock.context = {"rollup_pr_number": 8888, "rollup_pr_url": "https://x/8888"}
    rescue_session = AsyncMock()
    rescue_session.get = AsyncMock(return_value=db_parent_mock)
    rescue_session.add = MagicMock()
    rescue_session.commit = AsyncMock()
    rescue_ctx = MagicMock()
    rescue_ctx.__aenter__ = AsyncMock(return_value=rescue_session)
    rescue_ctx.__aexit__ = AsyncMock(return_value=False)
    mocker.patch("app.rollup_management.AsyncSessionLocal", return_value=rescue_ctx)

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db, project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/marker-exists", "parent_base": "main"},
        plan_output="some plan",
    )
    child = await _make_task(
        cleanup_db, project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    real_commit = cleanup_db.commit
    commit_calls = {"n": 0}
    # See companion test for the commit-index numbering after Phase B3.
    MARKER_COMMIT_INDEX = 3

    async def flaky_commit(*args, **kwargs):
        commit_calls["n"] += 1
        if commit_calls["n"] == MARKER_COMMIT_INDEX:
            raise RuntimeError("connection reset during marker commit")
        return await real_commit(*args, **kwargs)

    cleanup_db.commit = flaky_commit

    await _check_and_trigger_rollup(child, cleanup_db)

    # Rescue must not overwrite an existing marker
    rescue_session.add.assert_not_called()
    rescue_session.commit.assert_not_awaited()
    assert db_parent_mock.context == {
        "rollup_pr_number": 8888,
        "rollup_pr_url": "https://x/8888",
    }


# ── Error recovery: target/staging branch issues and all-cancelled ────────────

@pytest.mark.asyncio
async def test_target_branch_404_fails_parent(cleanup_db, mocker):
    """When get_branch_sha returns 404 for the target branch, the parent
    transitions to FAILED with a rollup-PR-creation-failed message (the 404
    raises a non-transient exception that the outer handler converts to FAILED)."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch("app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[])
    mocker.patch(
        "app.github.get_branch_sha",
        new_callable=AsyncMock,
        side_effect=HTTPException(status_code=404, detail="Not Found"),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db, project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={
            "research_branch": "research/stg-branch",
            "parent_base": "deleted-target-branch",
            "_rollup_retries": 99,  # ensure non-transient path runs immediately
        },
        plan_output="some plan",
    )
    child = await _make_task(
        cleanup_db, project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    assert "rollup PR creation failed" in (parent.latest_status_message or "")


@pytest.mark.asyncio
async def test_staging_branch_missing_all_children_completed_marks_completed(cleanup_db, mocker):
    """Staging branch is gone (404) but every child completed successfully —
    the work already landed on the target branch. Parent transitions to
    COMPLETED with a 'no actionable changes' message."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch("app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[])
    # Target branch exists; staging branch 404
    mocker.patch(
        "app.github.get_branch_sha",
        new_callable=AsyncMock,
        side_effect=[
            "target-sha",  # target_branch exists
            HTTPException(status_code=404, detail="Not Found"),  # staging missing
        ],
    )
    mocker.patch("app.github.delete_branch", new_callable=AsyncMock, return_value=True)

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db, project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={
            "research_branch": "research/gone-staging",
            "parent_base": "main",
        },
        plan_output="some plan",
    )
    child = await _make_task(
        cleanup_db, project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED
    assert "no actionable" in (parent.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_all_children_cancelled_marks_parent_cancelled(cleanup_db, mocker):
    """When every child is CANCELLED (none completed, none failed), the
    parent transitions to CANCELLED with a message noting all subtasks
    were cancelled. No rollup PR is created."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch("app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[])
    mock_create_pr = mocker.patch("app.github.create_pull_request", new_callable=AsyncMock)
    mocker.patch("app.github.delete_branch", new_callable=AsyncMock, return_value=True)

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db, project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={
            "research_branch": "research/all-cancelled",
            "parent_base": "main",
        },
        plan_output="some plan",
    )
    child1 = await _make_task(
        cleanup_db, project.id,
        parent_task_id=parent.id,
        status=TaskStatus.CANCELLED,
    )
    await _make_task(
        cleanup_db, project.id,
        parent_task_id=parent.id,
        status=TaskStatus.CANCELLED,
    )

    await _check_and_trigger_rollup(child1, cleanup_db)

    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.CANCELLED
    assert "cancelled" in (parent.latest_status_message or "").lower()
    mock_create_pr.assert_not_called()


# ── Rollup merge conflict state handling ──────────────────────────────────────
# Covers the mergeable_state state machine that runs after merge_pull_request
# returns merged=False on a rollup PR targeting an intermediate branch:
#   - 'dirty'    → real conflict, spawn fix task
#   - 'behind'   → update_branch; retry budget on failure, fall through when exhausted
#   - 'blocked'  → defer with counter, enable auto-merge
#   - 'unstable' → defer with counter, enable auto-merge
#   - Deferral counter crossing approval threshold flips requires_approval


async def _make_rollup_parent_with_pr(session, project_id, *, extra_context=None):
    """Rollup parent with an existing rollup_pr_number — exercises the resume
    path so the PR-creation code is skipped and control reaches the merge-phase.
    Target branch is intermediate (research/*) so the auto-merge branch runs."""
    ctx = {
        "research_branch": "research/conflict-stg",
        "parent_base": "research/conflict-base",
        "rollup_pr_number": 4242,
        "rollup_pr_url": "https://github.com/owner/repo/pull/4242",
    }
    if extra_context:
        ctx.update(extra_context)
    return await _make_task(
        session, project_id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context=ctx,
        plan_output="some plan",
    )


def _mock_rollup_merge_conflict_deps(mocker, *, mergeable_state, head_sha="deadbeef"):
    """Common mocks for reaching the mergeable_state state machine.

    get_pull_request is called three times with different semantics — plumb
    distinct responses via side_effect:
      1. existing-PR validation at resume (line ~725): open, not merged
      2. diff-size fetch (line ~840): small PR
      3. mergeable_state refetch after failed merge (line ~966)
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch("app.github.get_branch_sha", new_callable=AsyncMock, return_value="abc123")
    mocker.patch("app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[])
    mocker.patch(
        "app.security.scan_pr_for_injection",
        new_callable=AsyncMock,
        return_value={"blocked": False, "findings": [], "summary": "clean", "report": ""},
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": False, "message": "merge conflict"},
    )
    mocker.patch("app.github.get_pr_files", new_callable=AsyncMock, return_value=[])
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=[
            {"merged": False, "state": "open"},
            {"additions": 10, "deletions": 5, "merged": False, "state": "open"},
            {"mergeable_state": mergeable_state, "head": {"sha": head_sha}},
        ],
    )


@pytest.mark.asyncio
async def test_merge_conflict_dirty_spawns_fix_task(cleanup_db, mocker):
    """mergeable_state='dirty' → real conflict; spawn a fix task while parent
    remains EVALUATING, with _is_rollup_review=True marking the fix cycle."""
    _mock_rollup_merge_conflict_deps(mocker, mergeable_state="dirty")
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(MagicMock(id="fix-task-id"), None),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_task(
        cleanup_db, project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_spawn.assert_awaited_once()
    # First positional arg is the parent task; second is the pipeline task type.
    assert mock_spawn.await_args.args[1] == "fix"
    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    assert (parent.context or {}).get("_is_rollup_review") is True


@pytest.mark.asyncio
async def test_merge_conflict_behind_calls_update_branch_and_defers(cleanup_db, mocker):
    """mergeable_state='behind' + update_branch success → no fix task, parent
    defers to next sweep, behind-retry counter not set on success."""
    _mock_rollup_merge_conflict_deps(mocker, mergeable_state="behind")
    mock_update = mocker.patch(
        "app.github.update_branch_with_sha_retry",
        new_callable=AsyncMock,
        return_value={"updated": True, "message": "ok"},
    )
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, None),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_task(
        cleanup_db, project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_update.assert_awaited_once()
    mock_spawn.assert_not_called()
    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    assert "_behind_update_retries" not in (parent.context or {})


@pytest.mark.asyncio
async def test_merge_conflict_behind_update_fail_increments_retry_counter(cleanup_db, mocker):
    """mergeable_state='behind' + update_branch transient failure → defer with
    _behind_update_retries incremented, still within budget so no fix spawn."""
    _mock_rollup_merge_conflict_deps(mocker, mergeable_state="behind")
    mocker.patch(
        "app.github.update_branch_with_sha_retry",
        new_callable=AsyncMock,
        return_value={"updated": False, "message": "503 Service Unavailable"},
    )
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, None),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_task(
        cleanup_db, project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_spawn.assert_not_called()
    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    assert (parent.context or {}).get("_behind_update_retries") == 1


@pytest.mark.asyncio
async def test_merge_conflict_behind_budget_exhausted_falls_through_to_fix(cleanup_db, mocker):
    """Once _behind_update_retries increment equals the budget (default 3),
    the behind-path falls through to fix-spawn and clears the counter so a
    future behind state starts fresh."""
    _mock_rollup_merge_conflict_deps(mocker, mergeable_state="behind")
    mocker.patch(
        "app.github.update_branch_with_sha_retry",
        new_callable=AsyncMock,
        return_value={"updated": False, "message": "persistent failure"},
    )
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(MagicMock(id="fix-task-id"), None),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    # Pre-seeded at 2 so the increment becomes 3, == budget → fall-through path.
    parent = await _make_rollup_parent_with_pr(
        cleanup_db, project.id,
        extra_context={"_behind_update_retries": 2},
    )
    child = await _make_task(
        cleanup_db, project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_spawn.assert_awaited_once()
    assert mock_spawn.await_args.args[1] == "fix"
    await cleanup_db.refresh(parent)
    assert "_behind_update_retries" not in (parent.context or {})


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["blocked", "unstable"])
async def test_merge_conflict_blocked_or_unstable_defers_with_counter(cleanup_db, mocker, state):
    """mergeable_state in {'blocked','unstable'} → defer without spawning fix,
    increment per-state + total deferral counters, and enable GitHub auto-merge
    so the PR lands once gates clear without a full orchestrator sweep."""
    _mock_rollup_merge_conflict_deps(mocker, mergeable_state=state)
    mock_auto = mocker.patch(
        "app.github.enable_auto_merge",
        new_callable=AsyncMock,
        return_value={"enabled": True},
    )
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, None),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_task(
        cleanup_db, project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_auto.assert_awaited_once()
    mock_spawn.assert_not_called()
    await cleanup_db.refresh(parent)
    ctx = parent.context or {}
    assert parent.status == TaskStatus.EVALUATING
    assert ctx.get("_merge_deferred_state") == state
    assert ctx.get("_merge_deferred_count") == 1
    assert ctx.get("_merge_total_deferrals") == 1
    assert parent.requires_approval is False


@pytest.mark.asyncio
async def test_merge_conflict_deferral_counter_escalates_to_approval(cleanup_db, mocker):
    """Once per-state deferral count reaches rollup_merge_deferred_approval
    (default 30), the parent is marked requires_approval and the approval
    dispatch fires exactly once (the approval gate is latched by
    parent.requires_approval, so subsequent sweeps won't re-dispatch)."""
    _mock_rollup_merge_conflict_deps(mocker, mergeable_state="blocked")
    mocker.patch(
        "app.github.enable_auto_merge",
        new_callable=AsyncMock,
        return_value={"enabled": False, "message": "not eligible"},
    )
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, None),
    )
    mock_dispatch = mocker.patch(
        "app.rollup_management._dispatch_rollup_approval_alert",
        new_callable=AsyncMock,
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    # Pre-seed one below approval threshold (default 30); per-state alert
    # latch suppresses the 10-count alert, total-deferral latch suppresses
    # the 20-count total alert so neither competes with the approval assertion.
    parent = await _make_rollup_parent_with_pr(
        cleanup_db, project.id,
        extra_context={
            "_merge_deferred_state": "blocked",
            "_merge_deferred_count": 29,
            "_merge_total_deferrals": 29,
            "_merge_alerted_states": ["blocked"],
            "_merge_total_alerted": True,
        },
    )
    child = await _make_task(
        cleanup_db, project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(parent)
    ctx = parent.context or {}
    assert ctx.get("_merge_deferred_count") == 30
    assert parent.requires_approval is True
    mock_dispatch.assert_awaited_once()
    mock_spawn.assert_not_called()


# ── Tests for requeue limit and partial-rollup paths ──────────────────────────

@pytest.mark.asyncio
async def test_requeue_called_when_below_limit_with_mixed_children(cleanup_db, mocker):
    """When some children failed and _requeue_count < MAX_PARENT_REQUEUES (3),
    _requeue_parent_task is invoked instead of proceeding to partial rollup."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mock_requeue = mocker.patch(
        "app.task_lifecycle._requeue_parent_task", new_callable=AsyncMock
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db, project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={
            "_requeue_count": 1,  # below MAX_PARENT_REQUEUES (3)
            "plan_branch": "blueprint/plan-mixed",
            "parent_base": "main",
        },
        plan_output="some plan",
    )
    merged_child = await _make_task(
        cleanup_db, project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )
    failed_child = await _make_task(
        cleanup_db, project.id,
        parent_task_id=parent.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "CODE_ERROR"},
    )

    await _check_and_trigger_rollup(failed_child, cleanup_db)

    mock_requeue.assert_awaited_once()
    # Parent should not be FAILED — requeue path defers the decision
    await cleanup_db.refresh(parent)
    assert parent.status != TaskStatus.FAILED


@pytest.mark.asyncio
async def test_partial_rollup_proceeds_when_requeue_limit_exhausted(cleanup_db, mocker):
    """When _requeue_count == MAX_PARENT_REQUEUES (3) and some children merged
    while others failed, the rollup proceeds with the merged subset only —
    create_pull_request is invoked and _requeue_parent_task is NOT called."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch("app.github.get_branch_sha", new_callable=AsyncMock, return_value="abc123")
    mocker.patch("app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[])
    # empty_diff short-circuits the rollup to COMPLETED — keeps the test scope
    # to verifying that the PR-creation path was entered (i.e. partial rollup).
    mock_create_pr = mocker.patch(
        "app.github.create_pull_request",
        new_callable=AsyncMock,
        return_value={"empty_diff": True},
    )
    mocker.patch("app.github.delete_branch", new_callable=AsyncMock, return_value=True)
    mock_requeue = mocker.patch(
        "app.task_lifecycle._requeue_parent_task", new_callable=AsyncMock
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db, project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={
            "_requeue_count": 3,  # at MAX_PARENT_REQUEUES
            "plan_branch": "blueprint/plan-partial",
            "parent_base": "main",
        },
        plan_output="some plan",
    )
    merged_child = await _make_task(
        cleanup_db, project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )
    failed_child = await _make_task(
        cleanup_db, project.id,
        parent_task_id=parent.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "CODE_ERROR"},
    )

    await _check_and_trigger_rollup(failed_child, cleanup_db)

    mock_requeue.assert_not_awaited()
    mock_create_pr.assert_awaited_once()
    # Verify rollup PR head is the staging branch (only merged child's work)
    call_kwargs = mock_create_pr.call_args.kwargs
    assert call_kwargs.get("head") == "blueprint/plan-partial"
    await cleanup_db.refresh(parent)
    # empty_diff path completes the parent
    assert parent.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_parent_failure_stamps_deploy_monitoring(cleanup_db, mocker):
    """Path 5: when all children fail and the parent is marked FAILED inside
    _check_and_trigger_rollup, the failure must land in deploy_monitoring's
    task_results deque AND in the per-direction streak (when direction_id set)
    so anomaly detection and direction-hold logic can see the failure."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock, return_value=("token", "owner", "repo"),
    )

    from app.rollup_management import _check_and_trigger_rollup
    from app.routers.agent_status import _recorded_task_ids
    import app.shared_state as _ss

    # Reset deploy_monitoring + dedup so the test sees only its own writes
    _ss.deploy_monitoring["task_results"].clear()
    _recorded_task_ids.clear()
    _ss.direction_failure_streaks.clear()

    project = await _make_project(cleanup_db)
    direction_id = "dir-deploy-monitor-test"
    parent = await _make_task(
        cleanup_db, project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={
            "_requeue_count": 3,  # exhaust retries → fall through to FAILED
            "plan_branch": "blueprint/plan-monitor",
            "parent_base": "main",
            "direction_id": direction_id,
        },
        plan_output="some plan",
    )
    child = await _make_task(
        cleanup_db, project.id,
        parent_task_id=parent.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "CODE_ERROR"},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED

    # deploy_monitoring captured the parent failure
    results = list(_ss.deploy_monitoring["task_results"])
    assert any(r[1] == "failure" and r[2] == "CODE_ERROR" for r in results), \
        f"Parent FAILED never reached deploy_monitoring: {results}"

    # Per-direction streak incremented (CODE_ERROR is not infra → counts)
    streak = _ss.direction_failure_streaks.get(direction_id)
    assert streak is not None
    assert streak["consecutive_failures"] == 1
    assert streak["last_failure_class"] == "CODE_ERROR"


@pytest.mark.asyncio
async def test_parent_failure_after_pr_verification_stamps_deploy_monitoring(
    cleanup_db, mocker
):
    """Path 7 (line ~590): when _verify_pr_merged flips the only completed child
    to FAILED so completed_count drops to zero, the parent is marked FAILED in
    the all_done branch. That terminal path must also reach deploy_monitoring."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock, return_value=("token", "owner", "repo"),
    )
    # _verify_pr_merged returns True (so all_done loop completes) but the
    # mocked side-effect flips the child to FAILED before the rollup branch runs.
    async def _flip_to_failed(task, _session):
        task.status = TaskStatus.FAILED
        task.context = {**(task.context or {}), "failure_category": "AGENT_TIMEOUT"}
        return True
    mocker.patch(
        "app.rollup_management._verify_pr_merged",
        side_effect=_flip_to_failed,
    )

    from app.rollup_management import _check_and_trigger_rollup
    from app.routers.agent_status import _recorded_task_ids
    import app.shared_state as _ss

    _ss.deploy_monitoring["task_results"].clear()
    _recorded_task_ids.clear()

    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db, project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={
            "plan_branch": "blueprint/plan-flip",
            "parent_base": "main",
        },
        plan_output="some plan",
    )
    child = await _make_task(
        cleanup_db, project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={},  # _verify_pr_merged flips this to FAILED before all_done branch
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    results = list(_ss.deploy_monitoring["task_results"])
    assert any(r[1] == "failure" and r[2] == "AGENT_TIMEOUT" for r in results), \
        f"Post-verification parent FAILED never reached deploy_monitoring: {results}"


# ── High-risk approval gate (require_approval_gates) ──────────────────────────
# Covers rollup_management.py:846 — the gate that flips parent.requires_approval
# when a completed child has risk='high', the target is non-intermediate, and
# the runtime config opt-in is enabled. Refactor-type children are excluded
# from the high-risk set even when classified risk='high'.


def _mock_high_risk_gate_deps(mocker, *, get_pull_request_responses):
    """Common mocks for tests that exercise the high-risk approval gate.

    Drives the resume path (rollup_pr_number set on parent context) so
    create_pull_request is skipped and control reaches the gate at line 846.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.rollup_management._dispatch_rollup_approval_alert",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock, return_value=("token", "owner", "repo"),
    )
    mocker.patch("app.github.get_branch_sha", new_callable=AsyncMock, return_value="abc123")
    mocker.patch("app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[])
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=get_pull_request_responses,
    )


async def _make_rollup_parent_to_main(session, project_id):
    """Rollup parent on the resume path with target_branch='main' (non-intermediate)
    so the high-risk gate at line 846 is reachable."""
    return await _make_task(
        session, project_id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={
            "research_branch": "research/risky-stg",
            "parent_base": "main",
            "rollup_pr_number": 9999,
            "rollup_pr_url": "https://github.com/owner/repo/pull/9999",
        },
        plan_output="some plan",
    )


@pytest.mark.asyncio
async def test_high_risk_child_with_gate_enabled_requires_approval(cleanup_db, mocker):
    """Default require_approval_gates=True: a completed child with risk='high'
    on a non-intermediate target marks the rollup parent requires_approval=True
    and returns without merging."""
    _mock_high_risk_gate_deps(
        mocker,
        # Only the resume-path validation call fires before the gate returns.
        get_pull_request_responses=[{"merged": False, "state": "open"}],
    )
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock, return_value=(None, None),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_to_main(cleanup_db, project.id)
    child = await _make_task(
        cleanup_db, project.id,
        task_type="create",
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "risk": "high"},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(parent)
    assert parent.requires_approval is True
    assert "awaiting approval" in (parent.latest_status_message or "")
    # Gate returns before the review/fix spawn path
    mock_spawn.assert_not_called()


@pytest.mark.asyncio
async def test_high_risk_child_with_gate_disabled_skips_approval(
    cleanup_db, mocker, monkeypatch,
):
    """require_approval_gates=False: a completed high-risk child does NOT
    flip requires_approval; control falls through to the rollup-to-main
    review spawn."""
    from app.shared_state import runtime_config
    monkeypatch.setitem(runtime_config, "require_approval_gates", False)

    _mock_high_risk_gate_deps(
        mocker,
        get_pull_request_responses=[
            {"merged": False, "state": "open"},  # resume validation
            {"additions": 10, "deletions": 5, "merged": False, "state": "open"},  # diff size
        ],
    )
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(MagicMock(id="review-task-id"), None),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_to_main(cleanup_db, project.id)
    child = await _make_task(
        cleanup_db, project.id,
        task_type="create",
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "risk": "high"},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(parent)
    assert parent.requires_approval is False
    mock_spawn.assert_awaited_once()
    assert mock_spawn.await_args.args[1] == "review"


@pytest.mark.asyncio
async def test_refactor_high_risk_child_excluded_from_approval_gate(cleanup_db, mocker):
    """Refactor-type children are structural (no behavior change) and are
    excluded from the high-risk set even when marked risk='high'. Even with
    the default gate enabled, the parent is NOT flagged for approval and
    control falls through to the review spawn."""
    _mock_high_risk_gate_deps(
        mocker,
        get_pull_request_responses=[
            {"merged": False, "state": "open"},
            {"additions": 10, "deletions": 5, "merged": False, "state": "open"},
        ],
    )
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(MagicMock(id="review-task-id"), None),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_to_main(cleanup_db, project.id)
    # Refactor type with risk=high — the type-filter must exclude it.
    child = await _make_task(
        cleanup_db, project.id,
        task_type="refactor",
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "risk": "high"},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(parent)
    assert parent.requires_approval is False
    mock_spawn.assert_awaited_once()
    assert mock_spawn.await_args.args[1] == "review"
