"""
Tests for _cleanup_branches and _verify_pr_merged in rollup_management.py.

Covers _cleanup_branches:
  - Non-parent task detection (no plan_output, non-research/plan type) → no staging deletion
  - Guard 1: active children (QUEUED/PROVISIONING/IN_PROGRESS) → skip staging deletion
  - Guard 2: another EVALUATING task references same staging branch → skip
  - Guard 3: open PRs targeting the staging branch → skip
  - Guard 3: GitHub API error → skip (safe default)
  - Happy path: parent with no active children, no competing rollups, no open PRs → staging deleted
  - plan_output alone does NOT trigger staging deletion (regression: 2026-05-04)
  - plan child does NOT delete inherited research_branch (regression: 2026-05-04)
  - Feature branch cleanup: pr_merged=True with blueprint/ branch → feature branch deleted
  - Non-blueprint branch not deleted even with pr_merged=True
  - No staging branch in context → no deletion

Covers _verify_pr_merged:
  - Context cache hit (pr_merged=True already set) → returns True without API call
  - No PR artifact → caches pr_merged=True and returns True
  - GitHub API confirms merged → returns True, updates task.context
  - GitHub API 401 → invalidates token cache and retries with fresh token
  - Transient GitHub errors (500, 502, 503) → returns False without failing task
  - Transient failures (5xx, network error, rate-limit 403 remapped to 500) STAMP
    the negative cache → the next cycle inside the TTL issues zero GitHub calls
  - Closed-but-not-merged → marks task FAILED, creates alert, returns True
"""

import time

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

from app.rollup_management import _cleanup_branches, _verify_pr_merged
from app.rollup_pr_verification import _PR_MERGE_NEGATIVE_TTL_S
from app.rollup_size_preflight import SizeCapPreflightResult
from app.models import (
    Alert,
    AlertSeverity,
    Project,
    Task,
    TaskStatus,
    Artifact,
    ArtifactType,
)


# ── Fixture ───────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture()
async def cleanup_db():
    """Isolated in-memory SQLite DB for _cleanup_branches tests."""
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(
            type_, **kw
        )

    _engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    _Session = sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )

    async with _Session() as session:
        yield session

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await _engine.dispose()


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _make_project(
    session,
    *,
    integration_branch: str | None = None,
    promotion_target: str | None = None,
    build_config: dict | None = None,
):
    project = Project(
        name="Test Project",
        user_id="user1",
        integration_branch=integration_branch,
        promotion_target=promotion_target,
        build_config=build_config,
    )
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
        cleanup_db,
        project.id,
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
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/my-branch"},
    )
    await _make_task(
        cleanup_db,
        project.id,
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
        cleanup_db,
        project.id,
        task_type="plan",
        status=TaskStatus.EVALUATING,
        context={"plan_branch": "plan/test-branch"},
    )
    await _make_task(
        cleanup_db,
        project.id,
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
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/prov-branch"},
    )
    await _make_task(
        cleanup_db,
        project.id,
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
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/shared-branch"},
    )
    # All children completed — Guard 1 will pass
    await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
    )
    # Another EVALUATING rollup references this branch as parent_base
    await _make_task(
        cleanup_db,
        project.id,
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
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )

    await _cleanup_branches(parent, cleanup_db)

    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_guard2_original_base_reference_skips(cleanup_db, mocker):
    """Guard 2: another EVALUATING task references same staging branch via original_base → skip."""
    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db,
        project.id,
        task_type="plan",
        status=TaskStatus.EVALUATING,
        context={"plan_branch": "plan/shared-base"},
    )
    await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
    )
    # Another EVALUATING rollup references via original_base
    await _make_task(
        cleanup_db,
        project.id,
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
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )

    await _cleanup_branches(parent, cleanup_db)

    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_guard3_open_prs_skips_staging(cleanup_db, mocker):
    """Guard 3: open PRs targeting the staging branch → skip deletion."""
    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/pr-branch"},
    )
    await _make_task(
        cleanup_db,
        project.id,
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
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/error-branch"},
    )
    await _make_task(
        cleanup_db,
        project.id,
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
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/clean-branch"},
    )
    await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
    )

    mock_delete = mocker.patch(
        "app.github.delete_branch", new_callable=AsyncMock, return_value=True
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )

    await _cleanup_branches(parent, cleanup_db)

    mock_delete.assert_called_once_with(
        "gh-token", "owner", "repo", "research/clean-branch"
    )


@pytest.mark.asyncio
async def test_cleanup_happy_path_plan_branch_deleted(cleanup_db, mocker):
    """Happy path: plan parent with plan_branch context → plan_branch is deleted."""
    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db,
        project.id,
        task_type="plan",
        status=TaskStatus.EVALUATING,
        context={"plan_branch": "plan/finished-branch"},
    )
    # No children

    mock_delete = mocker.patch(
        "app.github.delete_branch", new_callable=AsyncMock, return_value=True
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )

    await _cleanup_branches(parent, cleanup_db)

    mock_delete.assert_called_once_with(
        "gh-token", "owner", "repo", "plan/finished-branch"
    )


@pytest.mark.asyncio
async def test_cleanup_plan_output_alone_does_not_delete_staging(cleanup_db, mocker):
    """Regression: plan_output presence alone (without task_type=research/plan)
    must not be enough to trigger staging deletion. plan_output and the staging
    branch context keys can both be inherited / stale; only the task's own
    role (task_type) determines what staging branch it owns."""
    project = await _make_project(cleanup_db)
    task = await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.EVALUATING,
        context={"plan_branch": "plan/inherited-from-parent"},
        plan_output={"steps": ["implement feature"]},
    )

    mock_delete = mocker.patch(
        "app.github.delete_branch", new_callable=AsyncMock, return_value=True
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )

    await _cleanup_branches(task, cleanup_db)

    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_plan_child_does_not_delete_inherited_research_branch(
    cleanup_db, mocker
):
    """Regression for incident 2026-05-04: cancelling a plan child of a research
    task must not delete the parent's research_branch.

    Context: plan children of research tasks inherit the parent's
    research_branch in their own context. The prior `plan_branch or research_branch`
    OR-fallback grabbed the inherited research_branch when the child's own
    plan_branch was unset (e.g. when _ensure_branch returned None before
    plan_branch could be written), then proceeded to delete the parent's
    branch — orphaning sibling work that had merged into it but not yet
    rolled up to main."""
    project = await _make_project(cleanup_db)
    plan_child = await _make_task(
        cleanup_db,
        project.id,
        task_type="plan",
        status=TaskStatus.CANCELLED,
        context={
            # research_branch INHERITED from research parent — not this task's own
            "research_branch": "research/parent-branch",
            # No plan_branch — _ensure_branch returned None before it was written
        },
    )

    mock_delete = mocker.patch(
        "app.github.delete_branch", new_callable=AsyncMock, return_value=True
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )

    await _cleanup_branches(plan_child, cleanup_db)

    # research_branch belongs to the research parent — must not be deleted.
    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_research_child_does_not_delete_inherited_plan_branch(
    cleanup_db, mocker
):
    """Symmetric regression: a research task that somehow has plan_branch in
    its context (inherited / stale) must not delete that plan_branch — research
    tasks own research_branch, never plan_branch."""
    project = await _make_project(cleanup_db)
    research_task = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.CANCELLED,
        context={
            "plan_branch": "plan/some-other-task",  # not owned by this task
            # No research_branch
        },
    )

    mock_delete = mocker.patch(
        "app.github.delete_branch", new_callable=AsyncMock, return_value=True
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )

    await _cleanup_branches(research_task, cleanup_db)

    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_feature_branch_deleted_for_merged_pr(cleanup_db, mocker):
    """Child task with pr_merged=True in context and a blueprint/ branch in payloads
    → the feature branch is deleted."""
    project = await _make_project(cleanup_db)
    child = await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[
            {
                "type": "create",
                "prompt": "create task",
                "branch": "blueprint/my-feature",
            }
        ],
    )

    mock_delete = mocker.patch(
        "app.github.delete_branch", new_callable=AsyncMock, return_value=True
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )

    await _cleanup_branches(child, cleanup_db)

    mock_delete.assert_called_once_with(
        "gh-token", "owner", "repo", "blueprint/my-feature"
    )


@pytest.mark.asyncio
async def test_cleanup_non_blueprint_branch_not_deleted(cleanup_db, mocker):
    """Feature branch that doesn't start with blueprint/ is NOT deleted
    even when pr_merged=True in context."""
    project = await _make_project(cleanup_db)
    child = await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
        payloads=[
            {
                "type": "create",
                "prompt": "create task",
                "branch": "feature/other-branch",
            }
        ],
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
        cleanup_db,
        project.id,
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
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/done-branch"},
    )
    # All children in terminal states
    await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
    )
    await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.FAILED,
        parent_task_id=parent.id,
    )
    await _make_task(
        cleanup_db,
        project.id,
        task_type="create",
        status=TaskStatus.CANCELLED,
        parent_task_id=parent.id,
    )

    mock_delete = mocker.patch(
        "app.github.delete_branch", new_callable=AsyncMock, return_value=True
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )

    await _cleanup_branches(parent, cleanup_db)

    mock_delete.assert_called_once_with(
        "gh-token", "owner", "repo", "research/done-branch"
    )


@pytest.mark.asyncio
async def test_cleanup_guard4_transitive_base_branch_targeter_skips_staging(
    cleanup_db, mocker
):
    """Guard 4 (incident regression): a non-terminal task that targets the
    staging branch via its payload ``base_branch`` — but is NOT a direct child
    of the parent (e.g. a grandchild fix task, child of an intermediate audit
    task) — must block staging deletion.

    Such a task falls through Guards 1-3: it is not a direct child
    (parent_task_id mismatch), it references the branch via payload base_branch
    rather than context parent_base/original_base, and it never opened a PR.
    The branch was deleted out from under these tasks → "Could not find remote
    branch research/<hex> to clone" → permanent failure.
    """
    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/abc12345"},
    )
    # Intermediate audit task (direct child) — terminal, so Guard 1 passes.
    audit = await _make_task(
        cleanup_db,
        project.id,
        task_type="audit",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
    )
    # Grandchild fix task — QUEUED, child of the audit task (NOT a direct child
    # of `parent`), targeting the staging branch via payload base_branch.
    await _make_task(
        cleanup_db,
        project.id,
        task_type="fix",
        status=TaskStatus.QUEUED,
        parent_task_id=audit.id,
        payloads=[
            {
                "type": "fix",
                "prompt": "fix the thing",
                "base_branch": "research/abc12345",
            }
        ],
    )

    mock_delete = mocker.patch(
        "app.github.delete_branch", new_callable=AsyncMock, return_value=True
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )

    await _cleanup_branches(parent, cleanup_db)

    # The grandchild still needs research/abc12345 to clone → must NOT be deleted.
    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_guard4_terminal_base_branch_targeter_still_deletes(
    cleanup_db, mocker
):
    """Guard 4 is not over-broad: a base_branch targeter that is TERMINAL
    (COMPLETED/FAILED/CANCELLED) does not keep the branch alive — with no other
    blockers the staging branch is still deleted."""
    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/def67890"},
    )
    audit = await _make_task(
        cleanup_db,
        project.id,
        task_type="audit",
        status=TaskStatus.COMPLETED,
        parent_task_id=parent.id,
    )
    # Grandchild fix task targeting the branch but already CANCELLED (terminal).
    await _make_task(
        cleanup_db,
        project.id,
        task_type="fix",
        status=TaskStatus.CANCELLED,
        parent_task_id=audit.id,
        payloads=[
            {
                "type": "fix",
                "prompt": "fix the thing",
                "base_branch": "research/def67890",
            }
        ],
    )

    mock_delete = mocker.patch(
        "app.github.delete_branch", new_callable=AsyncMock, return_value=True
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )

    await _cleanup_branches(parent, cleanup_db)

    mock_delete.assert_called_once_with(
        "gh-token", "owner", "repo", "research/def67890"
    )


@pytest.mark.asyncio
async def test_cleanup_guard4_scoped_to_project_not_cross_project(cleanup_db, mocker):
    """Guard 4 query is scoped to the parent task's project — a non-terminal task
    in a *different* project that happens to reference the same branch name does
    not block staging deletion."""
    # Project A: has the parent research task and no live grandchild targeter.
    project_a = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db,
        project_a.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/aabbccdd"},
    )

    # Project B: a separate project whose non-terminal task happens to carry the
    # same base_branch value — must NOT influence project A's cleanup.
    project_b = await _make_project(cleanup_db)
    await _make_task(
        cleanup_db,
        project_b.id,
        task_type="fix",
        status=TaskStatus.QUEUED,
        payloads=[
            {
                "type": "fix",
                "prompt": "unrelated fix",
                "base_branch": "research/aabbccdd",
            }
        ],
    )

    mock_delete = mocker.patch(
        "app.github.delete_branch", new_callable=AsyncMock, return_value=True
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("gh-token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )

    await _cleanup_branches(parent, cleanup_db)

    # The cross-project task must not block deletion of project A's branch.
    mock_delete.assert_called_once_with(
        "gh-token", "owner", "repo", "research/aabbccdd"
    )


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
    """The unblock bit short-circuits even when the task did not land on integration."""
    project = await _make_project(cleanup_db)
    task = await _make_task(
        cleanup_db,
        project.id,
        context={"pr_merged": True, "landed_on_integration": False},
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
    assert task.context.get("landed_on_integration") is False
    assert task.context.get("_non_merge_unblock_reason") == "rollup_no_pr_artifact"
    mock_get_pr.assert_not_called()


@pytest.mark.asyncio
async def test_verify_pr_merged_api_confirms_merged(cleanup_db, mocker):
    """A verified merged child PR is treated as an integration landing."""
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
    mocker.patch("app.rollup_pr_verification.AsyncSessionLocal", return_value=ctx)

    result = await _verify_pr_merged(task, cleanup_db)

    assert result is True
    assert task.context.get("pr_merged") is True
    assert task.context.get("landed_on_integration") is True
    assert "_non_merge_unblock_reason" not in task.context


@pytest.mark.asyncio
async def test_verify_pr_merged_records_exact_merge_sha(cleanup_db, mocker):
    """When the verified PR payload carries merge_commit_sha, _verify_pr_merged
    records it on the task so the discovery-review receipt can resolve exact
    commit identity (previously the landed signal was stamped without it)."""
    from app.landed_signal import LANDED_MERGE_SHA_KEY

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
        return_value={
            "merged": True,
            "state": "closed",
            "merge_commit_sha": "d" * 40,
        },
    )
    ctx, _ = _make_write_session_mock(db_task=None)
    mocker.patch("app.rollup_pr_verification.AsyncSessionLocal", return_value=ctx)

    result = await _verify_pr_merged(task, cleanup_db)

    assert result is True
    assert task.context.get(LANDED_MERGE_SHA_KEY) == "d" * 40
    assert task.context.get("landed_on_integration") is True
    assert "_non_merge_unblock_reason" not in task.context


@pytest.mark.asyncio
async def test_verify_pr_merged_on_distinct_promotion_target_alerts_without_pr_merged(
    cleanup_db, mocker
):
    project = await _make_project(
        cleanup_db,
        integration_branch="automain",
        promotion_target="main",
    )
    task = await _make_task(cleanup_db, project.id, context={})
    await _make_artifact(cleanup_db, task.id, "https://github.com/owner/repo/pull/43")

    mocker.patch(
        "app.broadcast.broadcast_to_project",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True, "state": "closed", "base": {"ref": "main"}},
    )
    write_ctx, _write_session = _make_write_session_mock(db_task=None)
    mocker.patch(
        "app.rollup_pr_verification.AsyncSessionLocal",
        return_value=write_ctx,
    )

    result = await _verify_pr_merged(task, cleanup_db)
    await cleanup_db.commit()
    alerts = (
        (await cleanup_db.execute(select(Alert).where(Alert.task_id == task.id)))
        .scalars()
        .all()
    )

    assert result is True
    assert (task.context or {}).get("pr_merged") is None
    assert write_ctx.__aenter__.await_count == 0
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR
    assert alerts[0].title == "Merged PR landed on promotion target before integration"


@pytest.mark.asyncio
async def test_verify_pr_merged_on_single_branch_sets_pr_merged(cleanup_db, mocker):
    project = await _make_project(
        cleanup_db,
        integration_branch="main",
        promotion_target="main",
    )
    task = await _make_task(cleanup_db, project.id, context={})
    await _make_artifact(cleanup_db, task.id, "https://github.com/owner/repo/pull/44")

    mocker.patch(
        "app.broadcast.broadcast_to_project",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True, "state": "closed", "base": {"ref": "main"}},
    )
    ctx, _ = _make_write_session_mock(db_task=None)
    mocker.patch("app.rollup_pr_verification.AsyncSessionLocal", return_value=ctx)

    result = await _verify_pr_merged(task, cleanup_db)
    await cleanup_db.commit()
    alerts = (
        (await cleanup_db.execute(select(Alert).where(Alert.task_id == task.id)))
        .scalars()
        .all()
    )

    assert result is True
    assert task.context.get("pr_merged") is True
    assert alerts == []


@pytest.mark.asyncio
async def test_verify_pr_merged_success_close_on_single_branch_sets_pr_merged(
    cleanup_db, mocker
):
    project = await _make_project(
        cleanup_db,
        integration_branch="main",
        promotion_target="main",
    )
    task = await _make_task(
        cleanup_db,
        project.id,
        context={"pr_closed_success": True},
    )
    await _make_artifact(cleanup_db, task.id, "https://github.com/owner/repo/pull/45")

    mocker.patch(
        "app.broadcast.broadcast_to_project",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": False, "state": "closed", "base": {"ref": "main"}},
    )
    db_task_mock = MagicMock()
    db_task_mock.context = {}
    db_task_mock.status = TaskStatus.EVALUATING
    ctx, write_session = _make_write_session_mock(db_task=db_task_mock)
    write_session.get = AsyncMock(return_value=db_task_mock)
    mocker.patch("app.rollup_pr_verification.AsyncSessionLocal", return_value=ctx)

    result = await _verify_pr_merged(task, cleanup_db)

    assert result is True
    assert task.status == TaskStatus.COMPLETED
    assert (task.context or {}).get("pr_merged") is True
    assert (task.context or {}).get("landed_on_integration") is True
    assert (task.context or {}).get("_non_merge_unblock_reason") is None
    assert db_task_mock.context.get("pr_merged") is True
    assert db_task_mock.context.get("landed_on_integration") is True
    assert db_task_mock.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_verify_pr_merged_success_close_on_distinct_promotion_target_alerts_without_pr_merged(
    cleanup_db, mocker
):
    project = await _make_project(
        cleanup_db,
        integration_branch="automain",
        promotion_target="main",
    )
    task = await _make_task(
        cleanup_db,
        project.id,
        context={"pr_closed_success": True},
    )
    await _make_artifact(cleanup_db, task.id, "https://github.com/owner/repo/pull/46")

    mocker.patch(
        "app.broadcast.broadcast_to_project",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": False, "state": "closed", "base": {"ref": "main"}},
    )
    db_task_mock = MagicMock()
    db_task_mock.context = {}
    db_task_mock.status = TaskStatus.EVALUATING
    ctx, write_session = _make_write_session_mock(db_task=db_task_mock)
    write_session.get = AsyncMock(return_value=db_task_mock)
    mocker.patch("app.rollup_pr_verification.AsyncSessionLocal", return_value=ctx)

    result = await _verify_pr_merged(task, cleanup_db)
    await cleanup_db.commit()
    alerts = (
        (await cleanup_db.execute(select(Alert).where(Alert.task_id == task.id)))
        .scalars()
        .all()
    )

    assert result is True
    assert task.status == TaskStatus.COMPLETED
    assert (task.context or {}).get("pr_merged") is None
    assert db_task_mock.context.get("pr_merged") is None
    assert db_task_mock.status == TaskStatus.COMPLETED
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR
    assert alerts[0].title == "Merged PR landed on promotion target before integration"


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
    mocker.patch("app.rollup_pr_verification.AsyncSessionLocal", return_value=ctx)

    result = await _verify_pr_merged(task, cleanup_db)

    assert result is True
    mock_invalidate.assert_called_once_with(str(task.project_id))
    # Second call must use force_refresh=True
    _, kwargs = mock_get_token.call_args_list[-1]
    assert kwargs.get("force_refresh") is True
    assert mock_get_pr.call_count == 2


@pytest.mark.asyncio
async def test_verify_pr_merged_401_retry_transient_negative_caches(cleanup_db, mocker):
    """When a 401 prompts a fresh-token retry and the retry hits a transient error
    (503), the negative cache IS stamped so the next dispatch cycle within the TTL
    issues NO further GitHub call.

    DELIBERATELY REVERSES TWO PRIOR MERGED FIXES — READ THIS BEFORE "RE-FIXING" IT.
    This test previously asserted the OPPOSITE ("must not poison negative cache").
    That was not an oversight: the no-cache-on-transient behavior was introduced on
    purpose, twice, on 2026-06-29:

      * PR #12281 "fix: explicit GitHub failure-path handling for rollup ancestry
        credit" — added the ``except Exception`` early return for non-HTTP (network)
        errors that skips the stamp. Its stated rationale was ONLY that stamping was
        "inconsistent with the HTTP 5xx path which already returned early without
        cache poisoning". That is an appeal to consistency with a path that was
        itself never justified — it argued from symmetry, not from behavior.
      * PR #12303 "fix: do not cache transient PR refresh failures" — wrapped the
        401 fresh-token retry so a transient failure there also skips the stamp.
        Its stated rationale was that stamping would "poison the negative cache,
        PREVENTING RETRIES and delaying rollup convergence".

    #12303's rationale is FACTUALLY FALSE, and that is why it is being reversed:
    ``_PR_MERGE_NEGATIVE_TTL_S = 30``. The stamp does not prevent retries — it
    delays exactly ONE retry by at most 30 seconds. (#12281's own body even calls it
    "the 30-second negative cache", so the TTL was known.) Neither PR cited an
    incident, a latency requirement, or considered call VOLUME.

    The cost of that mistaken belief: ``_check_candidate_task_deps`` re-verifies
    every same-base COMPLETED ``create`` dep of every candidate on every ~5s
    dispatch cycle, and the 30s negative cache is the ONLY throttle. Skipping the
    stamp on the transient class restored full 5s cadence — at up to 3 HTTP calls
    per verify (``_github_request_uncoalesced`` retries 5xx) — draining the shared
    5000/hr installation-token budget precisely when GitHub was already unhealthy.
    Worse, ``github_pulls.get_pull_request`` maps ANY non-200/401/404/410 to
    ``HTTPException(500)``, so a RATE-LIMIT 403 was classified transient and
    therefore NOT cached: the system's response to "you are out of quota" was to
    re-ask every 5 seconds.

    So the trade the first two passes made is real but tiny in one direction and
    unbounded in the other: caching costs AT MOST ~30s of extra merge-detection
    latency on a transient blip; not caching costs an unbounded call amplification
    against a shared budget whose exhaustion has twice stalled production. This is
    the third pass over this behavior; the first two walked it the wrong way.
    """
    project = await _make_project(cleanup_db)
    task = await _make_task(cleanup_db, project.id, context={})
    await _make_artifact(cleanup_db, task.id, "https://github.com/owner/repo/pull/77")

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("fresh-token", "owner", "repo"),
    )
    mock_invalidate = mocker.patch("app.routers.agent.invalidate_github_token_cache")
    # First call: 401 (auth expired)
    # Retry call: 503 (transient GitHub outage)
    mock_get_pr = mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=[
            HTTPException(status_code=401, detail="Unauthorized"),
            HTTPException(status_code=503, detail="Service Unavailable"),
        ],
    )
    from app.rollup_pr_verification import _pr_merge_negative_cache

    _pr_merge_negative_cache.pop(task.id, None)

    result = await _verify_pr_merged(task, cleanup_db)

    assert result is False
    assert task.id in _pr_merge_negative_cache, (
        "A transient error on the fresh-token retry must stamp the negative cache "
        "— otherwise the dep is re-verified every dispatch cycle"
    )
    mock_invalidate.assert_called_once_with(str(task.project_id))
    assert mock_get_pr.call_count == 2

    # Next dispatch cycle, inside the TTL: served from the negative cache, ZERO
    # additional GitHub calls (the whole point of the fix).
    assert await _verify_pr_merged(task, cleanup_db) is False
    assert mock_get_pr.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [500, 502, 503])
async def test_verify_pr_merged_transient_errors_return_false(
    cleanup_db, mocker, status_code
):
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
@pytest.mark.parametrize("status_code", [500, 502, 503])
async def test_verify_pr_merged_transient_error_negative_caches_next_cycle(
    cleanup_db, mocker, status_code
):
    """A transient GitHub 5xx STAMPS the negative cache: the next verification
    inside the 30s TTL issues NO GitHub call at all.

    Call-count assertion is the point — the pre-fix code returned False early
    WITHOUT stamping, so every ~5s dispatch cycle re-issued the lookup (each up
    to 3 HTTP calls, since ``_github_request_uncoalesced`` retries 5xx up to
    ``max_retries=3``) for every candidate's every same-base COMPLETED create dep.
    """
    project = await _make_project(cleanup_db)
    task = await _make_task(cleanup_db, project.id, context={})
    await _make_artifact(cleanup_db, task.id, "https://github.com/owner/repo/pull/98")

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mock_get_pr = mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=HTTPException(status_code=status_code, detail="Error"),
    )
    from app.rollup_pr_verification import _pr_merge_negative_cache

    _pr_merge_negative_cache.pop(task.id, None)

    assert await _verify_pr_merged(task, cleanup_db) is False
    assert mock_get_pr.call_count == 1
    assert task.id in _pr_merge_negative_cache

    # Cycles 2 and 3, both inside the TTL → still exactly ONE GitHub call total.
    assert await _verify_pr_merged(task, cleanup_db) is False
    assert await _verify_pr_merged(task, cleanup_db) is False
    assert mock_get_pr.call_count == 1

    # Once the TTL lapses the lookup resumes (recovery is not delayed beyond it).
    _pr_merge_negative_cache[task.id] = time.time() - _PR_MERGE_NEGATIVE_TTL_S - 1
    assert await _verify_pr_merged(task, cleanup_db) is False
    assert mock_get_pr.call_count == 2


@pytest.mark.asyncio
async def test_verify_pr_merged_network_error_negative_caches_next_cycle(
    cleanup_db, mocker
):
    """A non-HTTPException fetch error (network/DNS/timeout) is transient too, and
    must likewise stamp the negative cache instead of re-fetching every cycle."""
    project = await _make_project(cleanup_db)
    task = await _make_task(cleanup_db, project.id, context={})
    await _make_artifact(cleanup_db, task.id, "https://github.com/owner/repo/pull/97")

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mock_get_pr = mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=ConnectionError("connection reset by peer"),
    )
    from app.rollup_pr_verification import _pr_merge_negative_cache

    _pr_merge_negative_cache.pop(task.id, None)

    assert await _verify_pr_merged(task, cleanup_db) is False
    assert task.id in _pr_merge_negative_cache
    assert mock_get_pr.call_count == 1

    assert await _verify_pr_merged(task, cleanup_db) is False
    assert mock_get_pr.call_count == 1


@pytest.mark.asyncio
async def test_verify_pr_merged_rate_limit_403_negative_caches_next_cycle(
    cleanup_db, mocker
):
    """A GitHub rate-limit **403** is mapped by ``github_pulls.get_pull_request`` to
    an HTTPException(500) (any non-200/401/404 becomes a 500), so the transient
    classifier treats it as transient — meaning the un-cached class was BROADER
    than 5xx/network. Verify end-to-end (through the real ``get_pull_request``)
    that a 403 stamps the negative cache and the next cycle inside the TTL issues
    ZERO HTTP requests. Otherwise budget exhaustion self-amplifies: the response to
    "you are out of quota" was to ask again 720x/hr.
    """
    project = await _make_project(cleanup_db)
    task = await _make_task(cleanup_db, project.id, context={})
    await _make_artifact(cleanup_db, task.id, "https://github.com/owner/repo/pull/96")

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    rate_limited = MagicMock()
    rate_limited.status_code = 403
    rate_limited.text = "API rate limit exceeded"
    rate_limited.json = MagicMock(return_value={"message": "API rate limit exceeded"})
    # Patch the transport, NOT get_pull_request — the 403 → 500 remap under test
    # lives inside the real github_pulls.get_pull_request.
    mock_request = mocker.patch(
        "app.github._github_request",
        new_callable=AsyncMock,
        return_value=rate_limited,
    )
    from app.rollup_pr_verification import _pr_merge_negative_cache

    _pr_merge_negative_cache.pop(task.id, None)

    assert await _verify_pr_merged(task, cleanup_db) is False
    assert mock_request.call_count == 1
    assert task.id in _pr_merge_negative_cache, (
        "A rate-limit 403 (remapped to 500) must stamp the negative cache"
    )

    assert await _verify_pr_merged(task, cleanup_db) is False
    assert mock_request.call_count == 1


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
        # No bot author + no approval/supersession markers → FIX A must keep FAILED.
        return_value={"merged": False, "state": "closed"},
    )
    # No CHANGES_REQUESTED review, but also no positive cleanup signal — the
    # FIX A non-rejection classifier requires BOTH, so this stays FAILED.
    # NOTE: rollup_management calls get_pull_request_reviews_CHECKED, which returns
    # (reviews, status). Patching the list-only wrapper would VOID silently — the
    # patch succeeds, nothing calls it, and the real helper makes a live HTTP call.
    mocker.patch(
        "app.github.get_pull_request_reviews_checked",
        new_callable=AsyncMock,
        return_value=([], "complete"),
    )
    mocker.patch(
        "app.failure_classification.classify_failure",
        return_value="CODE_ERROR",
    )
    mocker.patch("app.routers.agent_status.record_task_result")
    mock_alert = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert",
        new_callable=AsyncMock,
    )

    db_task_mock = MagicMock()
    db_task_mock.context = {}
    ctx, write_session = _make_write_session_mock(db_task=db_task_mock)
    write_session.get = AsyncMock(return_value=db_task_mock)
    mocker.patch("app.rollup_pr_verification.AsyncSessionLocal", return_value=ctx)

    result = await _verify_pr_merged(task, cleanup_db)

    assert result is True
    assert task.status == TaskStatus.FAILED
    assert task.context.get("pr_closed_unmerged") is True
    assert "55" in task.latest_status_message
    mock_alert.assert_called_once()


# ── FIX A: non-rejection PR-close → CANCELLED ──────────────────────────────


def _mock_verify_pr_closed_deps(mocker, *, pr_data, reviews, reviews_status="complete"):
    """Common mocks for the closed-unmerged FIX A tests: token, PR fetch,
    reviews fetch, and a captured write_session so callers can assert the
    persisted terminal state. Returns (write_session, alert_mock).

    ``reviews_status`` is the enumeration outcome from
    ``get_pull_request_reviews_checked`` — "complete" / "truncated" / "failed".
    """
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value=pr_data,
    )
    # Must patch the _CHECKED variant: that is what rollup_management imports.
    # Patching the list-only wrapper voids silently and leaks a live HTTP call.
    mocker.patch(
        "app.github.get_pull_request_reviews_checked",
        new_callable=AsyncMock,
        return_value=(reviews, reviews_status),
    )
    mocker.patch("app.routers.agent_status.record_task_result")
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert",
        new_callable=AsyncMock,
    )
    db_task_mock = MagicMock()
    db_task_mock.context = {}
    ctx, write_session = _make_write_session_mock(db_task=db_task_mock)
    write_session.get = AsyncMock(return_value=db_task_mock)
    # _verify_pr_merged lives in app.rollup_pr_verification and writes via that
    # module's AsyncSessionLocal binding — patch there, not in rollup_management
    # (matches every other _verify_pr_merged test in this file). Patching the
    # wrong module let the real session run, which returned db_task=None against
    # the rollback-isolated fixture DB and silently skipped the failure alert.
    mocker.patch("app.rollup_pr_verification.AsyncSessionLocal", return_value=ctx)
    return write_session, alert_mock


@pytest.mark.asyncio
async def test_verify_pr_closed_after_approve_without_cleanup_signal_stays_failed(
    cleanup_db, mocker
):
    """Approval alone does not prove why a human-authored PR was closed.

    Without an explicit supersession/de-scope signal, a closed-unmerged PR must
    stay FAILED even when the old lifecycle flag and GitHub review say APPROVED.
    """
    project = await _make_project(cleanup_db)
    task = await _make_task(
        cleanup_db,
        project.id,
        context={"review_loop": {"review_approved": True, "pr_number": 55}},
    )
    await _make_artifact(cleanup_db, task.id, "https://github.com/owner/repo/pull/55")

    _, alert_mock = _mock_verify_pr_closed_deps(
        mocker,
        pr_data={"merged": False, "state": "closed", "user": {"login": "human"}},
        reviews=[{"state": "APPROVED"}],
    )

    result = await _verify_pr_merged(task, cleanup_db)

    assert result is True
    assert task.status == TaskStatus.FAILED
    assert task.context.get("failure_detail") != "pr_closed_non_rejection"
    alert_mock.assert_called_once()


@pytest.mark.asyncio
async def test_verify_pr_closed_with_explicit_supersession_lands_cancelled(
    cleanup_db, mocker
):
    """An explicit supersession marker remains a valid cleanup signal."""
    project = await _make_project(cleanup_db)
    task = await _make_task(
        cleanup_db,
        project.id,
        context={
            "superseded": True,
            "review_loop": {"review_approved": True, "pr_number": 55},
        },
    )
    await _make_artifact(cleanup_db, task.id, "https://github.com/owner/repo/pull/55")

    _, alert_mock = _mock_verify_pr_closed_deps(
        mocker,
        pr_data={"merged": False, "state": "closed", "user": {"login": "human"}},
        reviews=[{"state": "APPROVED"}],
    )

    result = await _verify_pr_merged(task, cleanup_db)

    assert result is True
    assert task.status == TaskStatus.CANCELLED
    assert task.context.get("failure_detail") == "pr_closed_non_rejection"
    alert_mock.assert_not_called()


@pytest.mark.asyncio
async def test_verify_pr_closed_by_bot_no_review_lands_cancelled(cleanup_db, mocker):
    """FIX A: a Blueprint-bot-authored PR closed-unmerged with no review at all
    (housekeeping close) lands CANCELLED, no alert."""
    project = await _make_project(cleanup_db)
    task = await _make_task(cleanup_db, project.id, context={})
    await _make_artifact(cleanup_db, task.id, "https://github.com/owner/repo/pull/55")

    _, alert_mock = _mock_verify_pr_closed_deps(
        mocker,
        pr_data={
            "merged": False,
            "state": "closed",
            "user": {"login": "blueprint-app[bot]", "type": "Bot"},
        },
        reviews=[],
    )

    result = await _verify_pr_merged(task, cleanup_db)

    assert result is True
    assert task.status == TaskStatus.CANCELLED
    assert task.context.get("failure_detail") == "pr_closed_non_rejection"
    alert_mock.assert_not_called()


@pytest.mark.asyncio
async def test_verify_pr_closed_with_changes_requested_stays_failed(cleanup_db, mocker):
    """GUARDRAIL: a CHANGES_REQUESTED review is a genuine rejection — even on a
    bot-authored PR, the task stays FAILED + alert."""
    project = await _make_project(cleanup_db)
    task = await _make_task(cleanup_db, project.id, context={})
    await _make_artifact(cleanup_db, task.id, "https://github.com/owner/repo/pull/55")

    mocker.patch(
        "app.failure_classification.classify_failure", return_value="CODE_ERROR"
    )
    _, alert_mock = _mock_verify_pr_closed_deps(
        mocker,
        pr_data={
            "merged": False,
            "state": "closed",
            "user": {"login": "blueprint-app[bot]", "type": "Bot"},
        },
        reviews=[{"state": "CHANGES_REQUESTED"}, {"state": "APPROVED"}],
    )

    result = await _verify_pr_merged(task, cleanup_db)

    assert result is True
    assert task.status == TaskStatus.FAILED, (
        "CHANGES_REQUESTED is a genuine rejection — must stay FAILED"
    )
    assert task.context.get("failure_detail") != "pr_closed_non_rejection"
    alert_mock.assert_called_once()


@pytest.mark.asyncio
async def test_verify_pr_closed_with_truncated_reviews_stays_failed(cleanup_db, mocker):
    """GUARDRAIL (fail closed on TRUNCATION): the reviews list came back truncated —
    we hold real pages and KNOW more reviews exist that we never saw. GitHub serves
    reviews oldest-first, so the missing ones are exactly the NEWEST — where a late
    CHANGES_REQUESTED would be. We cannot rule out a rejection, so a bot-authored
    cleanup-close (which would otherwise land CANCELLED) must STAY FAILED.

    Without this guard a rollup with >100 reviews that was genuinely rejected gets
    silently downgraded FAILED -> CANCELLED with no alert.
    """
    project = await _make_project(cleanup_db)
    task = await _make_task(cleanup_db, project.id, context={})
    await _make_artifact(cleanup_db, task.id, "https://github.com/owner/repo/pull/55")

    mocker.patch(
        "app.failure_classification.classify_failure", return_value="CODE_ERROR"
    )
    _, alert_mock = _mock_verify_pr_closed_deps(
        mocker,
        # Bot-authored + no CHANGES_REQUESTED in what we DID see => every other
        # signal points at CANCELLED. Only the truncation flag holds the line.
        pr_data={
            "merged": False,
            "state": "closed",
            "user": {"login": "blueprint-app[bot]", "type": "Bot"},
        },
        reviews=[{"state": "COMMENTED"}, {"state": "APPROVED"}],
        reviews_status="truncated",
    )

    result = await _verify_pr_merged(task, cleanup_db)

    assert result is True
    assert task.status == TaskStatus.FAILED, (
        "a TRUNCATED reviews list cannot rule out a CHANGES_REQUESTED — the newest "
        "reviews are precisely the ones missing, so the close must NOT be treated "
        "as a non-rejection"
    )
    assert task.context.get("failure_detail") != "pr_closed_non_rejection"
    alert_mock.assert_called_once()


@pytest.mark.asyncio
async def test_verify_pr_closed_with_failed_reviews_fetch_still_cancels(
    cleanup_db, mocker
):
    """The truncation guard must NOT fire on a mere FETCH FAILURE.

    A page-1 non-200 (rate limit / 502) enumerated NOTHING — that is absence of
    evidence, exactly like the 401 path, and the positive-cleanup-signal
    requirement already gates the verdict. Fail-closing here would let one
    rate-limited reviews fetch — the very failure mode this PR's premise is built
    on — flip a routine bot cleanup-close into a terminal FAILED + operator alert.

    This is the counterpart to test_verify_pr_closed_with_truncated_reviews_stays_failed:
    TRUNCATED and FAILED are different signals with opposite safe answers.
    """
    project = await _make_project(cleanup_db)
    task = await _make_task(cleanup_db, project.id, context={})
    await _make_artifact(cleanup_db, task.id, "https://github.com/owner/repo/pull/55")

    _, alert_mock = _mock_verify_pr_closed_deps(
        mocker,
        pr_data={
            "merged": False,
            "state": "closed",
            "user": {"login": "blueprint-app[bot]", "type": "Bot"},
        },
        reviews=[],
        reviews_status="failed",  # page-1 403/502: we saw nothing
    )

    result = await _verify_pr_merged(task, cleanup_db)

    assert result is True
    assert task.status == TaskStatus.CANCELLED, (
        "a transient reviews-fetch failure must NEVER on its own flip the verdict — "
        "the bot-close cleanup signal still stands, and fail-closing here would make "
        "a 502 stricter than a 401"
    )
    assert task.context.get("failure_detail") == "pr_closed_non_rejection"
    alert_mock.assert_not_called()


@pytest.mark.asyncio
async def test_verify_pr_closed_with_pr_rejected_marker_stays_failed(
    cleanup_db, mocker
):
    """GUARDRAIL: an explicit pr_rejected marker forces FAILED even with a
    bot author and no CHANGES_REQUESTED review (the reviews lookup is short-
    circuited by the marker)."""
    project = await _make_project(cleanup_db)
    task = await _make_task(cleanup_db, project.id, context={"pr_rejected": True})
    await _make_artifact(cleanup_db, task.id, "https://github.com/owner/repo/pull/55")

    mocker.patch(
        "app.failure_classification.classify_failure", return_value="CODE_ERROR"
    )
    _, alert_mock = _mock_verify_pr_closed_deps(
        mocker,
        pr_data={
            "merged": False,
            "state": "closed",
            "user": {"login": "blueprint-app[bot]", "type": "Bot"},
        },
        reviews=[],
    )

    result = await _verify_pr_merged(task, cleanup_db)

    assert result is True
    assert task.status == TaskStatus.FAILED
    assert task.context.get("failure_detail") != "pr_closed_non_rejection"
    alert_mock.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_url",
    [
        # Production incident 2026-05-06: haiku retry with wedged ./check viewport
        # gate improvised this URL into the wrapper's PR-URL marker file.
        "https://github.com/owner/repo/compare/main...blueprint/task-cb7bb07e",
        # Bare branch name
        "blueprint/task-cb7bb07e",
        # Plain text Claude might write under "I made changes, here's a description"
        "PR pending — see logs",
        # /pull/ path but trailing seg isn't numeric (defensive — shouldn't happen
        # for a real GitHub URL, but the guard is `/pull/ AND last_seg.isdigit()`)
        "https://github.com/owner/repo/pull/foo",
    ],
)
async def test_verify_pr_merged_malformed_pr_url_returns_false_without_calling_github(
    cleanup_db,
    mocker,
    bad_url,
):
    """Malformed PR URLs in the artifact must not crash int()-parsing — they
    fall through to the negative cache and the verifier returns False."""
    project = await _make_project(cleanup_db)
    task = await _make_task(cleanup_db, project.id, context={})
    await _make_artifact(cleanup_db, task.id, bad_url)

    mock_get_token = mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mock_get_pr = mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
    )

    result = await _verify_pr_merged(task, cleanup_db)

    assert result is False
    # Crucially: never reached the GitHub API or token fetch — the guard
    # short-circuits before either is called.
    mock_get_token.assert_not_called()
    mock_get_pr.assert_not_called()
    # Task is not flipped to FAILED on this path.
    assert task.status != TaskStatus.FAILED


# ── Tests for persist_failure_category at rollup parent-failure paths ─────────


@pytest.mark.asyncio
async def test_all_children_failed_parent_persists_failure_category(cleanup_db, mocker):
    """When all children fail and the rollup parent marks itself FAILED,
    failure_category must be set on parent.context (path 5 — all-children-failed).

    Children's categories are aggregated via worst_child_failure_category:
    CODE_ERROR > AGENT_TIMEOUT > infra classes.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
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
        cleanup_db,
        project.id,
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
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "AGENT_TIMEOUT"},
    )
    await _make_task(
        cleanup_db,
        project.id,
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
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)

    parent = await _make_task(
        cleanup_db,
        project.id,
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
        cleanup_db,
        project.id,
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
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    # Target branch exists, staging branch exists, no open PRs targeting staging
    mocker.patch(
        "app.github.get_branch_sha", new_callable=AsyncMock, return_value="abc123"
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )
    # Rollup PR is created (not empty_diff) but its diff fetch returns 0 lines changed
    mocker.patch(
        "app.github.create_pull_request",
        new_callable=AsyncMock,
        return_value={
            "number": 4242,
            "html_url": "https://github.com/owner/repo/pull/4242",
            "empty_diff": False,
        },
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"additions": 0, "deletions": 0, "merged": False, "state": "open"},
    )
    mock_close = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mock_delete = mocker.patch(
        "app.github.delete_branch", new_callable=AsyncMock, return_value=True
    )
    # Critical: verify review task is NOT spawned on zero-diff PR
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, None),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)

    parent = await _make_task(
        cleanup_db,
        project.id,
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
        cleanup_db,
        project.id,
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
    assert any(
        call.args[-1] == "research/zero-diff" for call in mock_delete.call_args_list
    )
    # No review/fix task spawned
    mock_spawn.assert_not_called()
    # Rollup PR marker cleared so reaper/retry logic doesn't resume with a closed PR
    assert "rollup_pr_number" not in (parent.context or {})


@pytest.mark.asyncio
async def test_zero_diff_rollup_on_intermediate_staged_target_stays_evaluating(
    cleanup_db, mocker
):
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_branch_sha", new_callable=AsyncMock, return_value="abc123"
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )
    mocker.patch(
        "app.github.create_pull_request",
        new_callable=AsyncMock,
        return_value={
            "number": 4243,
            "html_url": "https://github.com/owner/repo/pull/4243",
            "empty_diff": False,
        },
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"additions": 0, "deletions": 0, "merged": False, "state": "open"},
    )
    mock_close = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mock_delete = mocker.patch(
        "app.github.delete_branch", new_callable=AsyncMock, return_value=True
    )
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, None),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(
        cleanup_db,
        integration_branch="automain",
        promotion_target="release/main",
        build_config={"staging_branch_prefixes": ["release/"]},
    )

    parent = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={
            "research_branch": "release/zero-diff",
            "parent_base": "release/candidate-1",
        },
        payloads=[{"type": "research", "prompt": "rollup parent"}],
        plan_output="some plan",
    )
    child = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    assert (parent.context or {}).get("pr_merged") is None
    assert "awaiting downstream landing" in (parent.latest_status_message or "")
    mock_close.assert_awaited_once()
    assert any(
        call.args[-1] == "release/zero-diff" for call in mock_delete.call_args_list
    )
    mock_spawn.assert_not_called()
    assert "rollup_pr_number" not in (parent.context or {})


@pytest.mark.asyncio
async def test_unverified_pr_size_defers_instead_of_spawning_review(cleanup_db, mocker):
    """When get_pull_request fails transiently, defer the review-spawn decision
    to the next rollup cycle rather than assuming large and burning VM time on
    a PR whose size is unverified. Pre-2026-04-17 this path defaulted
    lines_changed=999 → review spawned on potentially-empty PRs → ~15
    VM-hours/day wasted per the audit."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_branch_sha", new_callable=AsyncMock, return_value="abc123"
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )
    mocker.patch(
        "app.github.create_pull_request",
        new_callable=AsyncMock,
        return_value={
            "number": 7070,
            "html_url": "https://github.com/owner/repo/pull/7070",
            "empty_diff": False,
        },
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=Exception("GitHub 502"),
    )
    mock_close = mocker.patch("app.github.close_pull_request", new_callable=AsyncMock)
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, None),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/unverified", "parent_base": "main"},
        plan_output="plan",
    )
    child = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
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
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_branch_sha", new_callable=AsyncMock, return_value="abc123"
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )
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
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/marker-fail", "parent_base": "main"},
        plan_output="some plan",
    )
    child = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    # Fail the marker commit inside _check_and_trigger_rollup. Lock-window-break
    # commits ahead of the marker commit (current state):
    #   1. _release_parent_lock before _verify_pr_merged loop (R5 audit fix)
    #   2. _release_parent_lock before get_open_pull_requests (read cluster)
    #   3. _release_parent_lock before create_pull_request
    #   4. The marker commit itself (the bug-target — failure here orphans the PR)
    # We let the first three commits succeed and only fail the marker so the
    # rescue path is what we observe.
    real_commit = cleanup_db.commit
    commit_calls = {"n": 0}
    MARKER_COMMIT_INDEX = 4

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
    assert (
        db_parent_mock.context.get("rollup_pr_url")
        == "https://github.com/owner/repo/pull/9999"
    )


@pytest.mark.asyncio
async def test_marker_commit_failure_skips_rescue_when_db_already_has_marker(
    cleanup_db, mocker
):
    """If the marker is already persisted in the DB (e.g. the commit partially
    succeeded or a prior cycle wrote it), the rescue path must NOT overwrite
    it — the existing value is authoritative."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_branch_sha", new_callable=AsyncMock, return_value="abc123"
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )
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
    db_parent_mock.context = {
        "rollup_pr_number": 8888,
        "rollup_pr_url": "https://x/8888",
    }
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
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/marker-exists", "parent_base": "main"},
        plan_output="some plan",
    )
    child = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    real_commit = cleanup_db.commit
    commit_calls = {"n": 0}
    # See companion test for the commit-index numbering after Phase B3 + R5
    # _verify_pr_merged lock-window-break.
    MARKER_COMMIT_INDEX = 4

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


@pytest.mark.asyncio
async def test_parent_mutated_during_pr_creation_runs_rescue_and_returns(
    cleanup_db, mocker
):
    """Production 2026-05-05: when ``_relock_parent`` returns None after a peer
    mutated the parent during ``create_pull_request``, the handler must (a)
    persist the new PR marker via the orphan-PR rescue path so the next sweep
    cycle resumes the existing PR, and (b) return without dereferencing
    ``parent`` — which is None on this path. Prior to the sentinel-class fix,
    the post-rescue retry-counter / status-flip code at line ~2020 NPE'd on
    ``parent.context`` and the rollup ``github_webhook`` returned 500.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_branch_sha", new_callable=AsyncMock, return_value="abc123"
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )
    mocker.patch(
        "app.github.create_pull_request",
        new_callable=AsyncMock,
        return_value={
            "number": 7777,
            "html_url": "https://github.com/owner/repo/pull/7777",
            "empty_diff": False,
        },
    )

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

    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={"research_branch": "research/relock-none", "parent_base": "main"},
        plan_output="some plan",
    )
    await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    # Force ``_relock_parent`` to return None on the call AFTER
    # ``create_pull_request`` (the third call in this flow: pre-verify,
    # pre-existing-PR-check, post-create). Returning None on the third
    # call simulates a peer mutating the parent during the PR creation
    # round-trip — the failure path the sentinel exception protects.
    real_relock = __import__(
        "app.rollup_management", fromlist=["_relock_parent"]
    )._relock_parent
    relock_calls = {"n": 0}

    async def relock_returns_none_on_post_create(*args, **kwargs):
        relock_calls["n"] += 1
        if relock_calls["n"] == 3:
            return None
        return await real_relock(*args, **kwargs)

    mocker.patch(
        "app.rollup_management._relock_parent",
        side_effect=relock_returns_none_on_post_create,
    )

    from app.rollup_management import _check_and_trigger_rollup

    # Must not raise — the AttributeError-on-NoneType bug surfaced as a
    # 500 from /api/github/webhook in production.
    child = (
        (
            await cleanup_db.execute(
                __import__("sqlmodel", fromlist=["select"])
                .select(Task)
                .where(Task.parent_task_id == parent.id)
            )
        )
        .scalars()
        .first()
    )
    await _check_and_trigger_rollup(child, cleanup_db)

    # Rescue must have persisted the marker via the fresh session — this
    # is what prevents a duplicate PR creation on the next sweep cycle.
    rescue_session.get.assert_awaited_once()
    get_args = rescue_session.get.await_args.args
    assert get_args[0] is Task
    assert get_args[1] == parent.id
    rescue_session.add.assert_called_once_with(db_parent_mock)
    rescue_session.commit.assert_awaited_once()
    assert db_parent_mock.context.get("rollup_pr_number") == 7777
    assert (
        db_parent_mock.context.get("rollup_pr_url")
        == "https://github.com/owner/repo/pull/7777"
    )


# ── Error recovery: target/staging branch issues and all-cancelled ────────────


@pytest.mark.asyncio
async def test_target_branch_404_fails_parent(cleanup_db, mocker):
    """When get_branch_sha returns 404 for the target branch, the parent
    transitions to FAILED with a rollup-PR-creation-failed message (the 404
    raises a non-transient exception that the outer handler converts to FAILED)."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new_callable=AsyncMock,
        side_effect=HTTPException(status_code=404, detail="Not Found"),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db,
        project.id,
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
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    assert "rollup PR creation failed" in (parent.latest_status_message or "")


@pytest.mark.asyncio
async def test_staging_branch_missing_all_children_completed_marks_completed(
    cleanup_db, mocker
):
    """Staging branch is gone (404) but every child completed successfully —
    the work already landed on the target branch. Parent transitions to
    COMPLETED with a 'no actionable changes' message."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )
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
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={
            "research_branch": "research/gone-staging",
            "parent_base": "main",
        },
        plan_output="some plan",
    )
    child = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED
    assert "no actionable" in (parent.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_all_children_genuinely_cancelled_marks_parent_failed(cleanup_db, mocker):
    """When every child is a GENUINE cancel (bare CANCELLED, none completed,
    none failed, no non-genuine markers), the rollup finalizer now routes
    through the shared terminal verdict and finalizes the parent FAILED +
    AGENT_TIMEOUT — matching the requeue/reaper path
    (test_requeue_all_cancelled_children_marks_parent_failed) instead of the old
    quiet CANCELLED that silently disagreed across sweeps. The verdict is now
    centralized — see test_parent_terminal_parity. No rollup PR is created."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mock_alert = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.task_lifecycle_requeue._record_parent_failure")
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )
    mock_create_pr = mocker.patch(
        "app.github.create_pull_request", new_callable=AsyncMock
    )
    mocker.patch("app.github.delete_branch", new_callable=AsyncMock, return_value=True)

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={
            "research_branch": "research/all-cancelled",
            "parent_base": "main",
        },
        plan_output="some plan",
    )
    child1 = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.CANCELLED,
    )
    await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.CANCELLED,
    )

    await _check_and_trigger_rollup(child1, cleanup_db)

    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    assert "cancelled" in (parent.latest_status_message or "").lower()
    # Genuine all-cancelled is a terminal failure in BOTH sweeps — alert fires.
    assert parent.context.get("failure_category") == "AGENT_TIMEOUT"
    mock_alert.assert_awaited()
    mock_create_pr.assert_not_called()


@pytest.mark.asyncio
async def test_infra_failed_review_awaiting_resurrection_keeps_parent_evaluating(
    cleanup_db, mocker
):
    """A recent infra-failed review is still live recovery work.

    The reaper may resurrect it once. Rollup must not terminalize the parent
    from the cancelled direct work child during the short FAILED -> QUEUED
    handoff window.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mock_alert = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mock_record_failure = mocker.patch(
        "app.task_lifecycle_requeue._record_parent_failure"
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db,
        project.id,
        task_type="perf",
        status=TaskStatus.EVALUATING,
        context={
            "research_branch": "research/infra-review-recovery",
            "parent_base": "main",
        },
        plan_output="some findings",
    )
    cancelled_child = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.CANCELLED,
    )
    await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.FAILED,
        context={
            "failure_category": "PROVISION_FAILURE",
            "_resurrection_count": 0,
        },
        payloads=[
            {
                "type": "review",
                "prompt": "Review the implementation PR",
                "pr_number": 15220,
            }
        ],
    )

    await _check_and_trigger_rollup(cancelled_child, cleanup_db)

    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    assert "auto-resurrection" in (parent.latest_status_message or "")
    assert parent.context.get("failure_category") is None
    mock_alert.assert_not_awaited()
    mock_record_failure.assert_not_called()


@pytest.mark.asyncio
async def test_exhausted_infra_review_does_not_defer_cancelled_parent(
    cleanup_db, mocker
):
    """Once the one-shot resurrection budget is spent, rollup must converge."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mock_alert = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.task_lifecycle_requeue._record_parent_failure")
    mocker.patch(
        "app.rollup_management._cleanup_branches", new_callable=AsyncMock
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_task(
        cleanup_db,
        project.id,
        task_type="perf",
        status=TaskStatus.EVALUATING,
        context={
            "research_branch": "research/exhausted-infra-review",
            "parent_base": "main",
        },
        plan_output="some findings",
    )
    cancelled_child = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.CANCELLED,
    )
    await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.FAILED,
        context={
            "failure_category": "PROVISION_FAILURE",
            "_resurrection_count": 1,
        },
        payloads=[
            {
                "type": "review",
                "prompt": "Review the implementation PR",
                "pr_number": 15220,
            }
        ],
    )

    await _check_and_trigger_rollup(cancelled_child, cleanup_db)

    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    assert "cancelled" in (parent.latest_status_message or "").lower()
    assert parent.context.get("failure_category") == "AGENT_TIMEOUT"
    mock_alert.assert_awaited()


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
        session,
        project_id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context=ctx,
        plan_output="some plan",
    )


def _mock_rollup_merge_conflict_deps(mocker, *, mergeable_state, head_sha="deadbeef"):
    """Common mocks for reaching the mergeable_state state machine.

    get_pull_request is called four times with different semantics — plumb
    distinct responses via side_effect:
      1. existing-PR validation at resume (line ~725): open, not merged
      2. diff-size fetch (line ~840): small PR
      3. exact-target verification immediately before the merge attempt
      4. mergeable_state refetch after failed merge (line ~966)
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_branch_sha", new_callable=AsyncMock, return_value="abc123"
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )
    mocker.patch(
        "app.security.scan_pr_for_injection",
        new_callable=AsyncMock,
        return_value={
            "blocked": False,
            "findings": [],
            "summary": "clean",
            "report": "",
        },
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
            {
                "additions": 10,
                "deletions": 5,
                "merged": False,
                "state": "open",
                "base": {"sha": "base-sha", "ref": "research/conflict-base"},
                "head": {"sha": head_sha, "ref": "research/conflict-stg"},
            },
            {
                "merged": False,
                "state": "open",
                "mergeable_state": "clean",
                "base": {"sha": "base-sha", "ref": "research/conflict-base"},
                "head": {"sha": head_sha, "ref": "research/conflict-stg"},
            },
            {
                "mergeable_state": mergeable_state,
                "base": {"sha": "base-sha", "ref": "research/conflict-base"},
                "head": {"sha": head_sha, "ref": "research/conflict-stg"},
            },
        ],
    )
    # Ensure pr_is_not_behind's ancestor check treats the PR as genuinely
    # behind so existing behind-path tests retain their update_branch assertion.
    mocker.patch(
        "app.github.is_ancestor_of",
        new_callable=AsyncMock,
        return_value=False,
    )
    async def _bound_ok(*_args, expected_head_sha=None, expected_base_sha=None, **_kwargs):
        return SizeCapPreflightResult(
            "ok", None, expected_head_sha, base_sha=expected_base_sha
        )

    mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        new_callable=AsyncMock,
        side_effect=_bound_ok,
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
        cleanup_db,
        project.id,
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
        cleanup_db,
        project.id,
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
async def test_merge_conflict_behind_update_fail_increments_retry_counter(
    cleanup_db, mocker
):
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
        cleanup_db,
        project.id,
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
async def test_merge_conflict_behind_budget_exhausted_falls_through_to_fix(
    cleanup_db, mocker
):
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
        cleanup_db,
        project.id,
        extra_context={"_behind_update_retries": 2},
    )
    child = await _make_task(
        cleanup_db,
        project.id,
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
async def test_merge_conflict_blocked_or_unstable_defers_with_counter(
    cleanup_db, mocker, state
):
    """mergeable_state in {'blocked','unstable'} → defer without spawning fix,
    increment per-state + total counters, and leave the retry to Blueprint."""
    _mock_rollup_merge_conflict_deps(mocker, mergeable_state=state)
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, None),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_spawn.assert_not_called()
    await cleanup_db.refresh(parent)
    ctx = parent.context or {}
    assert parent.status == TaskStatus.EVALUATING
    assert ctx.get("_merge_deferred_state") == state
    assert ctx.get("_merge_deferred_count") == 1
    assert ctx.get("_merge_total_deferrals") == 1
    assert parent.requires_approval is False


@pytest.mark.asyncio
async def test_merge_conflict_deferral_counter_escalates_to_approval(
    cleanup_db, mocker
):
    """Once per-state deferral count reaches rollup_merge_deferred_approval
    (default 30), the parent is marked requires_approval and the approval
    dispatch fires exactly once (the approval gate is latched by
    parent.requires_approval, so subsequent sweeps won't re-dispatch)."""
    _mock_rollup_merge_conflict_deps(mocker, mergeable_state="blocked")
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
        cleanup_db,
        project.id,
        extra_context={
            "_merge_deferred_state": "blocked",
            "_merge_deferred_count": 29,
            "_merge_total_deferrals": 29,
            "_merge_alerted_states": ["blocked"],
            "_merge_total_alerted": True,
        },
    )
    child = await _make_task(
        cleanup_db,
        project.id,
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
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
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
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={
            "_requeue_count": 1,  # below MAX_PARENT_REQUEUES (3)
            "plan_branch": "blueprint/plan-mixed",
            "parent_base": "main",
        },
        plan_output="some plan",
    )
    await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )
    failed_child = await _make_task(
        cleanup_db,
        project.id,
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
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_branch_sha", new_callable=AsyncMock, return_value="abc123"
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )
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
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={
            "_requeue_count": 3,  # at MAX_PARENT_REQUEUES
            "plan_branch": "blueprint/plan-partial",
            "parent_base": "main",
        },
        plan_output="some plan",
    )
    await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )
    failed_child = await _make_task(
        cleanup_db,
        project.id,
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
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
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
        cleanup_db,
        project.id,
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
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.FAILED,
        context={"failure_category": "CODE_ERROR"},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED

    # deploy_monitoring captured the parent failure
    results = list(_ss.deploy_monitoring["task_results"])
    assert any(r[1] == "failure" and r[2] == "CODE_ERROR" for r in results), (
        f"Parent FAILED never reached deploy_monitoring: {results}"
    )

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
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
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
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={
            "plan_branch": "blueprint/plan-flip",
            "parent_base": "main",
        },
        plan_output="some plan",
    )
    child = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={},  # _verify_pr_merged flips this to FAILED before all_done branch
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    results = list(_ss.deploy_monitoring["task_results"])
    assert any(r[1] == "failure" and r[2] == "AGENT_TIMEOUT" for r in results), (
        f"Post-verification parent FAILED never reached deploy_monitoring: {results}"
    )


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
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.rollup_management._dispatch_rollup_approval_alert",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_branch_sha", new_callable=AsyncMock, return_value="abc123"
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=get_pull_request_responses,
    )


async def _make_rollup_parent_to_main(session, project_id):
    """Rollup parent on the resume path with target_branch='main' (non-intermediate)
    so the high-risk gate at line 846 is reachable."""
    return await _make_task(
        session,
        project_id,
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
        new_callable=AsyncMock,
        return_value=(None, None),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_to_main(cleanup_db, project.id)
    child = await _make_task(
        cleanup_db,
        project.id,
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
    cleanup_db,
    mocker,
    monkeypatch,
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
            {
                "additions": 10,
                "deletions": 5,
                "merged": False,
                "state": "open",
            },  # diff size
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
        cleanup_db,
        project.id,
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
        cleanup_db,
        project.id,
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


# ===========================================================================
# ROOT CAUSE 4: malformed pr_url in rollup child → force-merge instead of stall
# ===========================================================================


@pytest.mark.asyncio
async def test_malformed_pr_url_artifact_force_merges_in_rollup(cleanup_db, mocker):
    """A child COMPLETED task whose PULL_REQUEST artifact carries a malformed
    pr_url (e.g. a /commit/<sha> URL) must NOT cause the rollup to hang.

    ROOT CAUSE 4 fix: the except clause in the unmerged-respawn loop now sets
    c.context['pr_merged'] = True so the next cycle can progress, mirroring the
    no-PR-artifact branch.
    """
    from datetime import datetime, timedelta, timezone
    from app.rollup_management import _check_and_trigger_rollup

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )

    project = await _make_project(cleanup_db)

    # Parent in EVALUATING — the rollup entry point.
    parent = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={
            "research_branch": "research/test-stg",
            "parent_base": "main",
        },
        plan_output="some plan",
    )

    # Child COMPLETED but pr_merged not set (the "unmerged" state).
    # updated_at must be > 10 min ago to pass the stale threshold check.
    stale_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=15)
    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "do thing"}],
        status=TaskStatus.COMPLETED,
        context={},
        updated_at=stale_time,
    )
    cleanup_db.add(child)
    await cleanup_db.commit()
    await cleanup_db.refresh(child)

    # Malformed pr_url — production incident used a /commit/<sha> URL.
    bogus_url = "https://github.com/HenryHamster/BlueprintAIStudio/commit/b8932d1"
    artifact = Artifact(
        task_id=child.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url=bogus_url,
    )
    cleanup_db.add(artifact)
    await cleanup_db.commit()

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(child)
    assert (child.context or {}).get("pr_merged") is True, (
        "Malformed pr_url must force-set pr_merged=True so rollup progresses"
    )
    assert (child.context or {}).get("landed_on_integration") is False
    assert (child.context or {}).get("_non_merge_unblock_reason") == "malformed_pr_url"


# ── ci_waiting seed gate for the spawn-review path ────────────────────────────
# Covers rollup_management.py:2584 — the "large rollup, spawn review" branch.
# .github/workflows/ci.yml triggers only on PRs into [main, automain]. A
# large intermediate-target rollup (research/* or plan/*) used to seed
# review_loop.ci_waiting=True, which the orchestrator then polled forever
# until the 1h CI-wait backstop killed the parent (incident: task
# 3d48cc53 / PR #6192, 2026-05-26). Intermediate rollups now skip the CI
# wait and rely on review-approval-only merge; the integration-branch
# path still gates on CI.


@pytest.mark.asyncio
async def test_large_intermediate_rollup_skips_ci_waiting(cleanup_db, mocker):
    """A >100-line rollup into research/* must NOT seed ci_waiting=True.

    ci.yml does not run on PRs into intermediate branches, so a CI wait
    is unsatisfiable and times out at the 1h backstop. The review task's
    APPROVE path merges directly (its is_ci_gated_merge check correctly
    skips CI for non-integration targets) and the next rollup sweep
    completes the parent.
    """
    _mock_high_risk_gate_deps(
        mocker,
        get_pull_request_responses=[
            {"merged": False, "state": "open"},  # resume validation
            {
                "additions": 200,
                "deletions": 0,
                "merged": False,
                "state": "open",
            },  # diff size — large
        ],
    )
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(MagicMock(id="review-task-id"), None),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    # _make_rollup_parent_with_pr uses parent_base="research/conflict-base"
    # (intermediate target).
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_spawn.assert_awaited_once()
    assert mock_spawn.await_args.args[1] == "review"
    await cleanup_db.refresh(parent)
    review_loop = (parent.context or {}).get("review_loop") or {}
    assert review_loop.get("ci_waiting") is not True
    assert "ci_wait_started" not in review_loop
    assert review_loop.get("pr_number") == 4242
    assert review_loop.get("base_branch") == "research/conflict-base"
    assert (parent.context or {}).get("_is_rollup_review") is True


@pytest.mark.asyncio
async def test_large_main_rollup_keeps_ci_waiting(cleanup_db, mocker):
    """A >100-line rollup into main must still seed ci_waiting=True.

    Integration-branch rollups have CI workflows that actually trigger, so
    the dual gate (ci_passed + review_approved) is needed to prevent a
    review-only merge from bypassing CI.
    """
    _mock_high_risk_gate_deps(
        mocker,
        get_pull_request_responses=[
            {"merged": False, "state": "open"},
            {
                "additions": 200,
                "deletions": 0,
                "merged": False,
                "state": "open",
            },
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
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_spawn.assert_awaited_once()
    assert mock_spawn.await_args.args[1] == "review"
    await cleanup_db.refresh(parent)
    review_loop = (parent.context or {}).get("review_loop") or {}
    assert review_loop.get("ci_waiting") is True
    assert review_loop.get("ci_wait_started")
    assert review_loop.get("base_branch") == "main"


@pytest.mark.asyncio
async def test_resume_review_preserves_inflight_ci_fix_state(cleanup_db, mocker):
    """PR #8746 (change C): RESUMING a large rollup whose EXISTING review_loop
    already records in-flight CI/fix progress (ci_fix_spawned set / ci_passed
    False) must PRESERVE that loop — not rebuild it and re-arm ci_waiting=True
    with a fresh ci_wait_started.

    Pre-fix the spawn-review path overwrote review_loop unconditionally,
    discarding ci_fix_spawned and resetting ci_wait_started to now → the 1h
    CI-wait backstop later FAILed the parent and cascade-cancelled the fix
    (research 5b250ecd). The original ci_wait_started and ci_fix_spawned must
    survive the resume.
    """
    _mock_high_risk_gate_deps(
        mocker,
        get_pull_request_responses=[
            {"merged": False, "state": "open"},  # resume validation
            {
                "additions": 200,
                "deletions": 0,
                "merged": False,
                "state": "open",
            },  # diff size — large → review path
        ],
    )
    mock_spawn = mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(MagicMock(id="review-task-id"), None),
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    original_ci_wait_started = "2026-06-07T12:00:00"
    parent = await _make_task(
        cleanup_db,
        project.id,
        task_type="research",
        status=TaskStatus.EVALUATING,
        context={
            "research_branch": "research/risky-stg",
            "parent_base": "main",
            "rollup_pr_number": 9999,
            "rollup_pr_url": "https://github.com/owner/repo/pull/9999",
            # Existing in-flight state from a prior pass: a CI-failure fix was
            # spawned, CI has not passed, the wait started earlier.
            "review_loop": {
                "pr_number": 9999,
                "base_branch": "main",
                "ci_waiting": True,
                "ci_wait_started": original_ci_wait_started,
                "ci_passed": False,
                "ci_fix_spawned": "fix-task-abc",
            },
        },
        plan_output="some plan",
    )
    child = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    mock_spawn.assert_awaited_once()
    assert mock_spawn.await_args.args[1] == "review"
    await cleanup_db.refresh(parent)
    review_loop = (parent.context or {}).get("review_loop") or {}
    # The existing in-flight state was preserved, NOT rebuilt/re-armed.
    assert review_loop.get("ci_fix_spawned") == "fix-task-abc"
    assert review_loop.get("ci_passed") is False
    assert review_loop.get("ci_wait_started") == original_ci_wait_started
    assert (parent.context or {}).get("_is_rollup_review") is True


# ── Tests for mergeable_state=None re-poll ────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_conflict_empty_mergeability_repoll_resolves_and_merges(
    cleanup_db, mocker
):
    """When mergeable_state is None/empty on initial refetch, the rollup path
    re-polls up to N times. If it resolves to 'clean' before the budget is
    exhausted, merge is retried in the same sweep and succeeds — no deferral,
    no fix spawn."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_branch_sha", new_callable=AsyncMock, return_value="abc123"
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )
    mocker.patch(
        "app.security.scan_pr_for_injection",
        new_callable=AsyncMock,
        return_value={
            "blocked": False,
            "findings": [],
            "summary": "clean",
            "report": "",
        },
    )
    mocker.patch("app.github.get_pr_files", new_callable=AsyncMock, return_value=[])
    mocker.patch(
        "app.github.is_ancestor_of", new_callable=AsyncMock, return_value=False
    )
    # get_pull_request calls:
    #  1. existing-PR validation (resume path)
    #  2. diff-size fetch
    #  3. authority boundary final GET → same exact, currently-safe target
    #  4. post-merge-failure refetch → mergeable_state=None (not yet computed)
    #  5. re-poll 1 → still empty
    #  6. re-poll 2 → "clean" (GitHub finished computing)
    #  7. retry authority boundary final GET → same exact clean target
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=[
            {
                "merged": False,
                "state": "open",
                "base": {
                    "sha": "basebeef",
                    "ref": "research/conflict-base",
                },
                "head": {
                    "sha": "deadbeef",
                    "ref": "research/conflict-stg",
                },
            },
            {
                "additions": 10,
                "deletions": 5,
                "merged": False,
                "state": "open",
                "base": {
                    "sha": "basebeef",
                    "ref": "research/conflict-base",
                },
                "head": {
                    "sha": "deadbeef",
                    "ref": "research/conflict-stg",
                },
            },
            {
                "merged": False,
                "state": "open",
                "mergeable": True,
                "mergeable_state": "clean",
                "base": {
                    "sha": "basebeef",
                    "ref": "research/conflict-base",
                },
                "head": {
                    "sha": "deadbeef",
                    "ref": "research/conflict-stg",
                },
            },
            {"mergeable_state": None, "head": {"sha": "deadbeef"}},
            {"mergeable_state": None, "head": {"sha": "deadbeef"}},
            {
                "mergeable_state": "clean",
                "base": {
                    "sha": "basebeef",
                    "ref": "research/conflict-base",
                },
                "head": {
                    "sha": "deadbeef",
                    "ref": "research/conflict-stg",
                },
            },
            {
                "merged": False,
                "state": "open",
                "mergeable": True,
                "mergeable_state": "clean",
                "base": {
                    "sha": "basebeef",
                    "ref": "research/conflict-base",
                },
                "head": {
                    "sha": "deadbeef",
                    "ref": "research/conflict-stg",
                },
            },
        ],
    )
    # merge_pull_request calls:
    #  1. initial merge attempt → fails (triggers the re-poll path)
    #  2. re-poll merge retry → succeeds
    mock_merge = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        side_effect=[
            {"merged": False, "message": "mergeable state is null"},
            {
                "merged": True,
                "sha": "abc",
                "message": "Pull Request successfully merged",
            },
        ],
    )
    mocker.patch("app.github.delete_branch", new_callable=AsyncMock, return_value=True)
    mocker.patch(
        "app.notification_dispatch.post_pr_merged_message", new_callable=AsyncMock
    )
    mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, None),
    )
    # Suppress the asyncio.sleep so the test runs instantly.
    mocker.patch("asyncio.sleep", new_callable=AsyncMock)
    async def _bound_ok(*_args, expected_head_sha=None, expected_base_sha=None, **_kwargs):
        return SizeCapPreflightResult(
            "ok", None, expected_head_sha, base_sha=expected_base_sha
        )

    mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        new_callable=AsyncMock,
        side_effect=_bound_ok,
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    # merge_pull_request must have been called twice: initial + re-poll retry.
    assert mock_merge.await_count == 2
    await cleanup_db.refresh(parent)
    # Parent moved to COMPLETED or EVALUATING (not stuck EVALUATING with deferred counters).
    assert parent.status in (TaskStatus.COMPLETED, TaskStatus.EVALUATING)
    # No deferral counters written — the re-poll resolved it.
    ctx = parent.context or {}
    assert "_merge_deferred_count" not in ctx
    assert "_merge_deferred_state" not in ctx


@pytest.mark.asyncio
async def test_merge_conflict_empty_mergeability_defers_when_repoll_exhausted(
    cleanup_db, mocker
):
    """When mergeable_state stays None/empty through all re-poll attempts, the
    sweep falls through to the normal deferral path — counters are bumped, no
    fix task is spawned."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_branch_sha", new_callable=AsyncMock, return_value="abc123"
    )
    mocker.patch(
        "app.github.get_open_pull_requests", new_callable=AsyncMock, return_value=[]
    )
    mocker.patch(
        "app.security.scan_pr_for_injection",
        new_callable=AsyncMock,
        return_value={
            "blocked": False,
            "findings": [],
            "summary": "clean",
            "report": "",
        },
    )
    mocker.patch("app.github.get_pr_files", new_callable=AsyncMock, return_value=[])
    mocker.patch(
        "app.github.is_ancestor_of", new_callable=AsyncMock, return_value=False
    )
    # All get_pull_request calls return empty mergeable_state.
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=[
            {
                "merged": False,
                "state": "open",
                "base": {
                    "sha": "basebeef",
                    "ref": "research/conflict-base",
                },
                "head": {
                    "sha": "deadbeef",
                    "ref": "research/conflict-stg",
                },
            },
            {
                "additions": 10,
                "deletions": 5,
                "merged": False,
                "state": "open",
                "base": {
                    "sha": "basebeef",
                    "ref": "research/conflict-base",
                },
                "head": {
                    "sha": "deadbeef",
                    "ref": "research/conflict-stg",
                },
            },
            {
                "merged": False,
                "state": "open",
                "mergeable": True,
                "mergeable_state": "clean",
                "base": {
                    "sha": "basebeef",
                    "ref": "research/conflict-base",
                },
                "head": {
                    "sha": "deadbeef",
                    "ref": "research/conflict-stg",
                },
            },
            # Post-merge-failure refetch + 3 re-polls all return empty state.
            {"mergeable_state": None, "head": {"sha": "deadbeef"}},
            {"mergeable_state": None, "head": {"sha": "deadbeef"}},
            {"mergeable_state": None, "head": {"sha": "deadbeef"}},
            {"mergeable_state": None, "head": {"sha": "deadbeef"}},
        ],
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": False, "message": "mergeable state is null"},
    )
    mocker.patch(
        "app.rollup_management._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, None),
    )
    mocker.patch("asyncio.sleep", new_callable=AsyncMock)
    async def _bound_ok(*_args, expected_head_sha=None, expected_base_sha=None, **_kwargs):
        return SizeCapPreflightResult(
            "ok", None, expected_head_sha, base_sha=expected_base_sha
        )

    mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        new_callable=AsyncMock,
        side_effect=_bound_ok,
    )

    from app.rollup_management import _check_and_trigger_rollup

    project = await _make_project(cleanup_db)
    parent = await _make_rollup_parent_with_pr(cleanup_db, project.id)
    child = await _make_task(
        cleanup_db,
        project.id,
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )

    await _check_and_trigger_rollup(child, cleanup_db)

    await cleanup_db.refresh(parent)
    ctx = parent.context or {}
    # Deferral counters written — empty state causes deferral.
    assert ctx.get("_merge_deferred_state") == "empty"
    assert ctx.get("_merge_deferred_count") == 1
    assert ctx.get("_merge_total_deferrals") == 1
    assert parent.status == TaskStatus.EVALUATING
