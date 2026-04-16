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
