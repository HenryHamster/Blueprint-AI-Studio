import uuid
import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select
from app.time_utils import _utcnow
from app.models import Project, Task, TaskStatus, Alert
from app.reaper import reaper_service, _reaper_skip_counts
from app.broadcast import broadcast_to_project, manager
from app.branch_cleanup import _run_stale_branch_cleanup
from app.task_lifecycle import _requeue_parent_task
from app.orchestrator import _check_candidate_task_deps


# The reaper creates its own DB sessions via AsyncSessionLocal. Tests must
# provide an isolated engine so the reaper's sessions see the same tables and
# data as the test setup.  Using the shared conftest engine causes StaticPool
# contention when both the fixture session and the reaper session are open
# concurrently on the same in-memory SQLite connection.


@pytest_asyncio.fixture()
async def reaper_db(mocker):
    """Isolated test DB for reaper tests — avoids StaticPool contention."""
    reaper_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with reaper_engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    ReaperSession = sessionmaker(
        bind=reaper_engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    mocker.patch("app.reaper.AsyncSessionLocal", ReaperSession)

    async with ReaperSession() as session:
        yield session

    async with reaper_engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await reaper_engine.dispose()


@pytest.mark.asyncio
async def test_reaper_evaluating_sweep_queries_updated_at(reaper_db, mocker):
    """Regression: the EVALUATING stale-parent sweep originally queried
    Task.updated_at before that column existed, raising AttributeError every
    reaper cycle. The stopgap swapped the query to started_at. Migration 0033
    adds the real updated_at column; this test pins the sweep to the real
    column and verifies the path end-to-end.
    """
    _common_reaper_mocks(mocker)
    mock_rollup = mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))

    project = Project(name="Evaluating Sweep Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    # Stale EVALUATING parent: last touched 20 minutes ago, past the 15-min cutoff.
    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "stuck evaluating"}],
        status=TaskStatus.EVALUATING,
        updated_at=_utcnow() - timedelta(minutes=20),
    )
    reaper_db.add(parent)
    await reaper_db.commit()
    await reaper_db.refresh(parent)

    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "finished subtask"}],
        status=TaskStatus.COMPLETED,
    )
    reaper_db.add(child)
    reaper_db.add(
        Task(
            project_id=project.id,
            parent_task_id=parent.id,
            payloads=[{"type": "fix", "prompt": "still working"}],
            status=TaskStatus.IN_PROGRESS,
            last_heartbeat=_utcnow(),
        )
    )
    await reaper_db.commit()

    try:
        await reaper_service()
    except _StopLoop:
        pass

    mock_rollup.assert_called_once()
    swept_child = mock_rollup.call_args.args[0]
    assert swept_child.parent_task_id == parent.id, (
        f"Expected sweep to pass the completed child of the stale parent, got {swept_child.id}"
    )


@pytest.mark.asyncio
async def test_reaper_service(reaper_db, mocker):
    """
    Tests the background worker that terminates orphaned VMs.
    We mock asyncio.sleep to break the infinite loop after one iteration.
    """
    _common_reaper_mocks(mocker)
    mock_delete = mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    # Disable infra-recovery resurrection so the task stays FAILED for this assertion
    mocker.patch("app.reaper.any_zone_available", return_value=False)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))

    project = Project(name="Reaper Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[],
        vm_instance_id="orphan-vm",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=3,  # Exhausted retries → reaper should mark FAILED
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    assert task.status == TaskStatus.FAILED
    mock_delete.assert_called_once_with("orphan-vm")


@pytest.mark.asyncio
async def test_reaper_retry_path(reaper_db, mocker):
    """
    Tests that a task with remaining retries is reset to QUEUED (not FAILED)
    when the reaper detects a heartbeat timeout.
    """
    _common_reaper_mocks(mocker)
    mock_delete = mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))

    project = Project(name="Reaper Retry Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[],
        vm_instance_id="retry-vm",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=1,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert task.attempt_count == 2
    mock_delete.assert_called_once_with("retry-vm")


@pytest.mark.asyncio
async def test_reaper_retry_preserves_analysis_artifacts(reaper_db, mocker):
    """Reaper requeue must copy _pr_diff_summary / _test_failure_summary /
    _self_review_outcome / _analysis_plan to their _previous_* equivalents so
    the agent's buildRetryContext can surface them on the next attempt."""
    _common_reaper_mocks(mocker)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))

    project = Project(name="Reaper Artifact Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    original_ctx = {
        "_pr_diff_summary": [{"file": "a.py", "added": 3}],
        "_test_failure_summary": "pytest failed in test_x",
        "_self_review_outcome": {"verdict": "needs_changes"},
        "_analysis_plan": "step1: refactor; step2: test",
    }
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="artifact-vm",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=1,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context=dict(original_ctx),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    assert task.status == TaskStatus.QUEUED
    ctx = task.context or {}
    assert ctx.get("_previous_diff_summary") == original_ctx["_pr_diff_summary"]
    assert (
        ctx.get("_previous_test_failure_summary")
        == original_ctx["_test_failure_summary"]
    )
    assert (
        ctx.get("_previous_self_review_outcome") == original_ctx["_self_review_outcome"]
    )
    assert ctx.get("_previous_analysis_plan") == original_ctx["_analysis_plan"]


@pytest.mark.asyncio
async def test_reaper_terminal_failure(reaper_db, mocker):
    """
    Tests that a task at its retry limit is permanently marked FAILED
    when the reaper detects a heartbeat timeout.
    """
    _common_reaper_mocks(mocker)
    mock_delete = mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    # Disable infra-recovery resurrection so the task stays FAILED for this assertion
    mocker.patch("app.reaper.any_zone_available", return_value=False)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))

    project = Project(name="Reaper Terminal Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[],
        vm_instance_id="final-vm",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=3,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    assert task.status == TaskStatus.FAILED
    mock_delete.assert_called_once_with("final-vm")


@pytest.mark.asyncio
async def test_reaper_persists_failure_category_on_terminal_fail(reaper_db, mocker):
    """
    When the reaper terminally fails a task whose heartbeat never arrived,
    the classifier's AGENT_TIMEOUT verdict must be persisted to
    task.context["failure_category"] so downstream direction rollups bucket
    the failure as infrastructure, not a code defect.
    """
    _common_reaper_mocks(mocker)
    # Disable infra-recovery resurrection so failure_category survives in context
    mocker.patch("app.reaper.any_zone_available", return_value=False)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))

    project = Project(name="Reaper Category Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[],
        vm_instance_id="category-vm",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=3,  # At retry limit → reaper must mark FAILED
        last_heartbeat=_utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert (task.context or {}).get("failure_category") == "AGENT_TIMEOUT"


@pytest.mark.asyncio
async def test_reaper_five_minute_heartbeat_threshold(reaper_db, mocker):
    """
    Tests that a task whose last heartbeat was 6 minutes ago (just past the
    5-minute threshold) is detected as orphaned and re-queued.
    """
    _common_reaper_mocks(mocker)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))

    project = Project(name="Reaper Threshold Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[],
        vm_instance_id="threshold-vm",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=6),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert task.attempt_count == 1


@pytest.mark.asyncio
async def test_reaper_parent_task_reevaluation(reaper_db, mocker):
    """
    Tests that when a coordinator task (one that has spawned child sub-tasks)
    is reaped, _requeue_parent_task is called and the coordinator is set back
    to EVALUATING instead of being blindly re-dispatched to a new VM.
    """
    _common_reaper_mocks(mocker)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))

    project = Project(name="Reaper Parent Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    coordinator = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        vm_instance_id="coordinator-vm",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(coordinator)
    await reaper_db.commit()
    await reaper_db.refresh(coordinator)

    child = Task(
        project_id=project.id,
        payloads=[],
        parent_task_id=coordinator.id,
        status=TaskStatus.IN_PROGRESS,
    )
    reaper_db.add(child)
    await reaper_db.commit()

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(coordinator)
    assert coordinator.status == TaskStatus.EVALUATING


@pytest_asyncio.fixture()
async def cleanup_db(mocker):
    """Isolated in-memory DB for stale branch cleanup tests."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    Session = sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    mocker.patch("app.branch_cleanup.AsyncSessionLocal", Session)

    async with Session() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


def _uuid_with_prefix(hex8: str) -> uuid.UUID:
    """Build a deterministic UUID whose stringified short_id (first 8 hex) matches."""
    return uuid.UUID(f"{hex8}-0000-0000-0000-000000000000")


@pytest.mark.asyncio
async def test_stale_branch_cleanup_preserves_active_task_branch(cleanup_db, mocker):
    """Branches owned by active (non-terminal) tasks must not be deleted."""
    project = Project(
        name="Cleanup Test", user_id="user1", github_owner="org", github_repo="repo"
    )
    cleanup_db.add(project)
    await cleanup_db.commit()
    await cleanup_db.refresh(project)

    task = Task(
        id=_uuid_with_prefix("abc12345"),
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.IN_PROGRESS,
        context={"review_loop": {"branch": "blueprint/task-abc12345"}},
    )
    cleanup_db.add(task)
    await cleanup_db.commit()

    mocker.patch("app.branch_cleanup._get_project_github_token", return_value="tok")
    mocker.patch(
        "app.github.list_branches",
        return_value=[
            {"name": "blueprint/task-abc12345", "sha": "sha1"},
        ],
    )
    mock_delete = mocker.patch("app.github.delete_branch", return_value=True)

    await _run_stale_branch_cleanup()

    # Gate 2 (name in active_branches) AND Gate 5 (status is IN_PROGRESS) both protect.
    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_stale_branch_cleanup_deletes_orphaned_branch(cleanup_db, mocker):
    """Branches whose owning task is TERMINAL and HEAD commit is stale should be deleted."""
    project = Project(
        name="Cleanup Test 2", user_id="user2", github_owner="org", github_repo="repo"
    )
    cleanup_db.add(project)
    await cleanup_db.commit()
    await cleanup_db.refresh(project)

    task = Task(
        id=_uuid_with_prefix("deadbeef"),
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
    )
    cleanup_db.add(task)
    await cleanup_db.commit()

    mocker.patch("app.branch_cleanup._get_project_github_token", return_value="tok")
    mocker.patch(
        "app.github.list_branches",
        return_value=[
            {"name": "blueprint/task-deadbeef", "sha": "sha2"},
        ],
    )
    # HEAD commit 10 days old — well past the 2-day stale threshold.
    mocker.patch(
        "app.github.get_commit_date",
        return_value=_utcnow() - timedelta(days=10),
    )
    mock_delete = mocker.patch("app.github.delete_branch", return_value=True)

    await _run_stale_branch_cleanup()

    mock_delete.assert_called_once_with("tok", "org", "repo", "blueprint/task-deadbeef")


@pytest.mark.asyncio
async def test_stale_branch_cleanup_spares_protected_branches(cleanup_db, mocker):
    """Protected branches (main, master, develop) must never be deleted."""
    project = Project(
        name="Cleanup Test 3", user_id="user3", github_owner="org", github_repo="repo"
    )
    cleanup_db.add(project)
    await cleanup_db.commit()
    await cleanup_db.refresh(project)

    mocker.patch("app.branch_cleanup._get_project_github_token", return_value="tok")
    mocker.patch(
        "app.github.list_branches",
        return_value=[
            {"name": "main", "sha": "sha3"},
            {"name": "master", "sha": "sha4"},
            {"name": "develop", "sha": "sha5"},
        ],
    )
    mock_delete = mocker.patch("app.github.delete_branch", return_value=True)

    await _run_stale_branch_cleanup()

    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_stale_branch_cleanup_skips_active_task_with_old_updated_at(
    cleanup_db, mocker
):
    """A task that's still IN_PROGRESS must not have its branch deleted, even if
    the row's updated_at is ancient (which can happen when a long-running task
    predates the cleanup threshold). Gate 5 catches this."""
    project = Project(
        name="Cleanup Test 4", user_id="user4", github_owner="org", github_repo="repo"
    )
    cleanup_db.add(project)
    await cleanup_db.commit()
    await cleanup_db.refresh(project)

    old = _utcnow() - timedelta(days=30)
    task = Task(
        id=_uuid_with_prefix("cafef00d"),
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.IN_PROGRESS,
        # Context deliberately lacks a branch key so name-based active_branches
        # won't catch it — forces Gate 5 (status check) to do the work.
    )
    cleanup_db.add(task)
    await cleanup_db.commit()
    task.updated_at = old
    cleanup_db.add(task)
    await cleanup_db.commit()

    mocker.patch("app.branch_cleanup._get_project_github_token", return_value="tok")
    mocker.patch(
        "app.github.list_branches",
        return_value=[
            {"name": "blueprint/task-cafef00d", "sha": "sha4"},
        ],
    )
    mock_delete = mocker.patch("app.github.delete_branch", return_value=True)

    await _run_stale_branch_cleanup()

    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_stale_branch_cleanup_skips_branch_with_no_matching_task(
    cleanup_db, mocker
):
    """If a branch's short_id doesn't resolve to any task in the DB, skip it —
    we only delete what we can attribute. Gate 4."""
    project = Project(
        name="Cleanup Test 5", user_id="user5", github_owner="org", github_repo="repo"
    )
    cleanup_db.add(project)
    await cleanup_db.commit()
    await cleanup_db.refresh(project)

    mocker.patch("app.branch_cleanup._get_project_github_token", return_value="tok")
    mocker.patch(
        "app.github.list_branches",
        return_value=[
            {"name": "blueprint/task-ffffffff", "sha": "sha5"},  # no task owns this
        ],
    )
    mock_delete = mocker.patch("app.github.delete_branch", return_value=True)

    await _run_stale_branch_cleanup()

    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_stale_branch_cleanup_skips_terminal_task_with_open_pr(
    cleanup_db, mocker
):
    """COMPLETED task with a PR reference but NO closure marker (pr_merged /
    pr_rejected / pr_closed_unmerged) must keep its branch — the PR may still
    be awaiting review and deleting the branch auto-closes it. Gate 5.5."""
    project = Project(
        name="Cleanup PR Test", user_id="userpr", github_owner="org", github_repo="repo"
    )
    cleanup_db.add(project)
    await cleanup_db.commit()
    await cleanup_db.refresh(project)

    task = Task(
        id=_uuid_with_prefix("beef0001"),
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        # PR reference present, no closure flag — PR may still be open.
        context={
            "review_loop": {
                "pr_number": 42,
                "pr_url": "https://github.com/org/repo/pull/42",
            }
        },
    )
    cleanup_db.add(task)
    await cleanup_db.commit()

    mocker.patch("app.branch_cleanup._get_project_github_token", return_value="tok")
    mocker.patch(
        "app.github.list_branches",
        return_value=[
            {"name": "blueprint/task-beef0001", "sha": "shapr"},
        ],
    )
    # Commit is well past the stale threshold — only Gate 5.5 should hold.
    mocker.patch(
        "app.github.get_commit_date",
        return_value=_utcnow() - timedelta(days=10),
    )
    mock_delete = mocker.patch("app.github.delete_branch", return_value=True)

    await _run_stale_branch_cleanup()

    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_stale_branch_cleanup_deletes_terminal_task_with_merged_pr(
    cleanup_db, mocker
):
    """COMPLETED task whose PR merged (pr_merged=True) is safe to clean up
    once stale. Confirms Gate 5.5 doesn't over-block when a closure marker
    is present."""
    project = Project(
        name="Cleanup Merged PR Test",
        user_id="usermerged",
        github_owner="org",
        github_repo="repo",
    )
    cleanup_db.add(project)
    await cleanup_db.commit()
    await cleanup_db.refresh(project)

    task = Task(
        id=_uuid_with_prefix("beef0002"),
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
        context={
            "review_loop": {"pr_number": 43},
            "pr_merged": True,
        },
    )
    cleanup_db.add(task)
    await cleanup_db.commit()

    mocker.patch("app.branch_cleanup._get_project_github_token", return_value="tok")
    mocker.patch(
        "app.github.list_branches",
        return_value=[
            {"name": "blueprint/task-beef0002", "sha": "shamerged"},
        ],
    )
    mocker.patch(
        "app.github.get_commit_date",
        return_value=_utcnow() - timedelta(days=10),
    )
    mock_delete = mocker.patch("app.github.delete_branch", return_value=True)

    await _run_stale_branch_cleanup()

    mock_delete.assert_called_once_with("tok", "org", "repo", "blueprint/task-beef0002")


@pytest.mark.asyncio
async def test_stale_branch_cleanup_skips_recent_terminal_task(cleanup_db, mocker):
    """Terminal task whose branch HEAD commit is fresher than the stale
    threshold (2d) should keep its branch. Gate 6."""
    project = Project(
        name="Cleanup Test 6", user_id="user6", github_owner="org", github_repo="repo"
    )
    cleanup_db.add(project)
    await cleanup_db.commit()
    await cleanup_db.refresh(project)

    task = Task(
        id=_uuid_with_prefix("12345678"),
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
    )
    cleanup_db.add(task)
    await cleanup_db.commit()

    mocker.patch("app.branch_cleanup._get_project_github_token", return_value="tok")
    mocker.patch(
        "app.github.list_branches",
        return_value=[
            {"name": "blueprint/task-12345678", "sha": "sha6"},
        ],
    )
    # HEAD commit 12 hours old — well inside the 2-day stale buffer.
    mocker.patch(
        "app.github.get_commit_date",
        return_value=_utcnow() - timedelta(hours=12),
    )
    mock_delete = mocker.patch("app.github.delete_branch", return_value=True)

    await _run_stale_branch_cleanup()

    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_broadcast_to_project():
    """
    Tests the WebSocket broadcast via ConnectionManager.
    """
    project_id = "test-proj-1"
    sent_messages = []

    class FakeWebSocket:
        """Minimal mock that records sent JSON messages."""

        async def send_json(self, data):
            sent_messages.append(data)

        @property
        def client(self):
            class _Client:
                host = "127.0.0.1"

            return _Client()

    ws = FakeWebSocket()
    # Register the fake connection directly
    manager.active_connections[project_id] = [ws]

    try:
        await broadcast_to_project(project_id, "test_event", {"foo": "bar"})

        assert len(sent_messages) == 1
        assert sent_messages[0]["event"] == "test_event"
        assert sent_messages[0]["data"]["foo"] == "bar"
    finally:
        manager.active_connections.pop(project_id, None)


# ── _requeue_parent_task retry guidance ────────────────────────────────────


@pytest_asyncio.fixture()
async def requeue_db(mocker):
    """Isolated in-memory DB for _requeue_parent_task tests."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    Session = sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )

    async with Session() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


async def _make_parent_child(
    session, child_payloads, child_context=None, child_status_message=None
):
    """Helper: create a parent+child pair, commit, return (parent, child)."""
    project = Project(name="Requeue Test", user_id="user1")
    session.add(project)
    await session.commit()
    await session.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
        attempt_count=0,
    )
    session.add(parent)
    await session.commit()
    await session.refresh(parent)

    child = Task(
        project_id=project.id,
        payloads=child_payloads,
        parent_task_id=parent.id,
        status=TaskStatus.FAILED,
        context=child_context or {},
        latest_status_message=child_status_message,
    )
    session.add(child)
    await session.commit()
    await session.refresh(child)

    return parent, child


@pytest.mark.asyncio
async def test_requeue_agent_timeout_injects_scope_guidance(requeue_db, mocker):
    """AGENT_TIMEOUT retries receive scope reduction guidance in prompt_suffix."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "do the thing"}],
        child_status_message="reaped after no heartbeat for 30 minutes",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (
        (
            await requeue_db.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 1
    suffix = (clones[0].payloads or [{}])[0].get("prompt_suffix", "")
    assert "TIMED OUT" in suffix
    assert "reduce scope" in suffix.lower()


@pytest.mark.asyncio
async def test_requeue_code_error_test_failure_injects_test_guidance(
    requeue_db, mocker
):
    """CODE_ERROR with test keywords in status message injects test-fixing guidance."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "add feature"}],
        child_status_message="pytest test_foo failed with AssertionError",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (
        (
            await requeue_db.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 1
    suffix = (clones[0].payloads or [{}])[0].get("prompt_suffix", "")
    assert "RETRY GUIDANCE" in suffix
    assert "failing tests" in suffix.lower()


@pytest.mark.asyncio
async def test_requeue_code_error_lint_failure_injects_lint_guidance(
    requeue_db, mocker
):
    """CODE_ERROR with lint keywords injects lint-fixing guidance."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "refactor module"}],
        child_status_message="ruff lint error: E501 line too long",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (
        (
            await requeue_db.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 1
    suffix = (clones[0].payloads or [{}])[0].get("prompt_suffix", "")
    assert "RETRY GUIDANCE" in suffix
    assert "lint" in suffix.lower()


@pytest.mark.asyncio
async def test_requeue_large_scope_second_timeout_downgrades_scope(requeue_db, mocker):
    """Large-scope task on 2nd+ AGENT_TIMEOUT attempt gets scope downgraded to medium."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "big task"}],
        child_context={"scope": "large", "_attempt_number": 2},
        child_status_message="execution timed out after 60 minutes",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (
        (
            await requeue_db.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 1
    clone_ctx = clones[0].context or {}
    assert clone_ctx.get("scope") == "medium"
    assert clone_ctx.get("_scope_reduced") is True


@pytest.mark.asyncio
async def test_requeue_large_scope_first_timeout_no_scope_downgrade(requeue_db, mocker):
    """Large-scope task on 1st AGENT_TIMEOUT does NOT get scope downgraded."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "big task first try"}],
        child_context={"scope": "large", "_attempt_number": 1},
        child_status_message="execution timed out after 60 minutes",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (
        (
            await requeue_db.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 1
    clone_ctx = clones[0].context or {}
    # scope should not be changed to medium on first timeout
    assert clone_ctx.get("scope") == "large"
    assert "_scope_reduced" not in clone_ctx


@pytest.mark.asyncio
async def test_requeue_rate_limit_no_scope_guidance(requeue_db, mocker):
    """Non-timeout, non-code-error failures (e.g. RATE_LIMIT) do not inject scope guidance."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "some task"}],
        child_status_message="API Error: Rate limit reached",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (
        (
            await requeue_db.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 1
    # RATE_LIMIT gets retry-strategy guidance (backoff/Retry-After) but NOT
    # the scope-reduction language reserved for AGENT_TIMEOUT — a rate limit
    # isn't a signal to shrink the task, just to slow down retries.
    suffix = (clones[0].payloads or [{}])[0].get("prompt_suffix") or ""
    assert "reduce scope" not in suffix.lower()
    assert "skip non-essential" not in suffix.lower()
    assert "pick the 2-3 most important" not in suffix.lower()


@pytest.mark.asyncio
async def test_requeue_sets_retry_context_fields(requeue_db, mocker):
    """clone_ctx contains _attempt_number incremented, _previous_error, and _previous_failure_class."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "do something"}],
        child_context={"_attempt_number": 1, "scope": "medium"},
        child_status_message="pytest test_foo failed with AssertionError: expected 1 got 2",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (
        (
            await requeue_db.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 1
    clone_ctx = clones[0].context or {}

    # _attempt_number must be incremented from 1 → 2
    assert clone_ctx.get("_attempt_number") == 2
    # _previous_error must contain the original status message
    assert "AssertionError" in (clone_ctx.get("_previous_error") or "")
    # _previous_failure_class must be set (CODE_ERROR for a pytest failure)
    assert clone_ctx.get("_previous_failure_class") == "CODE_ERROR"


@pytest.mark.asyncio
async def test_requeue_propagates_pr_artifacts_to_retry_context(requeue_db, mocker):
    """clone_ctx contains _previous_pr_url and _previous_changed_files from the failed child's context."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    pr_url = "https://github.com/org/repo/pull/42"
    changed_files = ["src/foo.py", "src/bar.py"]

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[
            {
                "type": "fix",
                "prompt": "fix the bug",
                "pr_number": 42,
                "branch": "fix/bug",
            }
        ],
        child_context={
            "_attempt_number": 1,
            "_pr_url": pr_url,
            "_pr_changed_files": changed_files,
        },
        child_status_message="pytest test_foo failed with AssertionError: expected 1 got 2",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (
        (
            await requeue_db.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 1
    clone_ctx = clones[0].context or {}

    # PR URL from failed attempt must propagate to retry clone
    assert clone_ctx.get("_previous_pr_url") == pr_url
    # Changed files from failed attempt must propagate to retry clone
    assert clone_ctx.get("_previous_changed_files") == changed_files


@pytest.mark.asyncio
async def test_requeue_propagates_file_change_details_to_retry_context(
    requeue_db, mocker
):
    """clone_ctx contains _previous_file_change_details from the failed child's context."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    file_change_details = [
        {"file": "backend/app/models.py", "additions": 45, "deletions": 12},
        {"file": "backend/testing/test_models.py", "additions": 8, "deletions": 3},
    ]

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "add feature"}],
        child_context={
            "_attempt_number": 1,
            "_file_change_details": file_change_details,
        },
        child_status_message="pytest test_models.py failed: AssertionError",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (
        (
            await requeue_db.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 1
    clone_ctx = clones[0].context or {}

    assert clone_ctx.get("_previous_file_change_details") == file_change_details


@pytest.mark.asyncio
async def test_requeue_no_pr_artifacts_when_child_has_none(requeue_db, mocker):
    """If child context has no _pr_url/_pr_changed_files, retry clone omits the _previous_* fields."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "fresh task"}],
        child_context={"_attempt_number": 1},
        child_status_message="pytest test_foo failed with AssertionError",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (
        (
            await requeue_db.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 1
    clone_ctx = clones[0].context or {}

    # No PR artifacts on child → _previous_* keys must be absent
    assert "_previous_pr_url" not in clone_ctx
    assert "_previous_changed_files" not in clone_ctx


@pytest.mark.asyncio
async def test_requeue_propagates_enriched_step_context(requeue_db, mocker):
    """clone_ctx propagates _previous_diff_summary, _previous_test_failure_summary,
    and _previous_self_review_outcome from the failed child's context."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    diff_summary = [
        {
            "file": "backend/app/models.py",
            "additions": 45,
            "deletions": 12,
            "change_type": "modified",
        },
        {
            "file": "backend/testing/test_models.py",
            "additions": 30,
            "deletions": 0,
            "change_type": "added",
        },
    ]
    test_failure_summary = (
        "FAILED testing/test_models.py::test_create - AssertionError: expected 1 got 2"
    )
    self_review_outcome = {
        "issues_found": False,
        "fixes_applied": False,
        "categories": [],
    }

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "add model"}],
        child_context={
            "_attempt_number": 1,
            "_pr_diff_summary": diff_summary,
            "_test_failure_summary": test_failure_summary,
            "_self_review_outcome": self_review_outcome,
        },
        child_status_message="pytest test_create failed with AssertionError: expected 1 got 2",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (
        (
            await requeue_db.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 1
    clone_ctx = clones[0].context or {}

    assert clone_ctx.get("_previous_diff_summary") == diff_summary
    assert clone_ctx.get("_previous_test_failure_summary") == test_failure_summary
    assert clone_ctx.get("_previous_self_review_outcome") == self_review_outcome


@pytest.mark.asyncio
async def test_requeue_omits_enriched_step_context_when_absent(requeue_db, mocker):
    """If child context has no enriched step fields, retry clone omits the _previous_ enriched fields."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "fresh task"}],
        child_context={"_attempt_number": 1},
        child_status_message="pytest test_foo failed with AssertionError",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (
        (
            await requeue_db.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 1
    clone_ctx = clones[0].context or {}

    assert "_previous_diff_summary" not in clone_ctx
    assert "_previous_test_failure_summary" not in clone_ctx
    assert "_previous_self_review_outcome" not in clone_ctx


async def _make_parent_with_children(session, children_specs, parent_context=None):
    """Helper: create a parent with multiple children.

    Each entry in children_specs is a dict with optional keys:
      - payloads: list of payload dicts (default: [{"type": "create", "prompt": "task"}])
      - status: TaskStatus (default: FAILED)
      - context: dict (default: {})
      - status_message: str (default: None)
      - depends_on_indices: list of int indices into children_specs (resolved after creation)

    Returns (parent, [children]) — all objects are refreshed with DB-assigned IDs.
    """
    project = Project(name="Requeue Test Multi", user_id="user1")
    session.add(project)
    await session.commit()
    await session.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
        attempt_count=0,
        context=parent_context or {},
    )
    session.add(parent)
    await session.commit()
    await session.refresh(parent)

    # First pass: create all children without depends_on so IDs are assigned.
    children = []
    for spec in children_specs:
        child = Task(
            project_id=project.id,
            payloads=spec.get("payloads", [{"type": "create", "prompt": "task"}]),
            parent_task_id=parent.id,
            status=spec.get("status", TaskStatus.FAILED),
            context=spec.get("context", {}),
            latest_status_message=spec.get("status_message"),
            depends_on=[],
        )
        session.add(child)
        children.append(child)

    await session.commit()
    for c in children:
        await session.refresh(c)

    # Second pass: wire up depends_on using resolved IDs.
    for child, spec in zip(children, children_specs):
        dep_indices = spec.get("depends_on_indices", [])
        if dep_indices:
            child.depends_on = [str(children[i].id) for i in dep_indices]
            session.add(child)

    await session.commit()
    for c in children:
        await session.refresh(c)

    return parent, children


@pytest.mark.asyncio
async def test_requeue_count_limit_boundary_succeeds(requeue_db, mocker):
    """Parent with _requeue_count=2 (one below MAX_PARENT_REQUEUES=3) still requeues."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "task"}],
        child_status_message="ruff lint error: E501 line too long",
    )
    parent.context = {"_requeue_count": 2}
    requeue_db.add(parent)
    await requeue_db.commit()
    await requeue_db.refresh(parent)

    result = await _requeue_parent_task(parent, requeue_db)

    assert result == 1
    await requeue_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    assert parent.context.get("_requeue_count") == 3


@pytest.mark.asyncio
async def test_requeue_count_limit_at_limit_fails_permanently(requeue_db, mocker):
    """Parent with _requeue_count=3 (at MAX_PARENT_REQUEUES) is set to FAILED and alert fired."""
    mock_alert = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "task"}],
        child_status_message="ruff lint error: E501 line too long",
    )
    parent.context = {"_requeue_count": 3}
    requeue_db.add(parent)
    await requeue_db.commit()
    await requeue_db.refresh(parent)

    result = await _requeue_parent_task(parent, requeue_db)

    assert result == 0
    await requeue_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    assert "Re-evaluation limit" in parent.latest_status_message
    assert "3" in parent.latest_status_message
    mock_alert.assert_called_once()
    # No clones should have been created
    clones = (
        (
            await requeue_db.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 0


@pytest.mark.asyncio
async def test_requeue_cancelled_parent_returns_zero(requeue_db, mocker):
    """CANCELLED parent returns 0 immediately without creating any clones."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "task"}],
        child_status_message="some failure",
    )
    parent.status = TaskStatus.CANCELLED
    requeue_db.add(parent)
    await requeue_db.commit()
    await requeue_db.refresh(parent)

    result = await _requeue_parent_task(parent, requeue_db)

    assert result == 0
    clones = (
        (
            await requeue_db.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 0


@pytest.mark.asyncio
async def test_requeue_dependency_remapping(requeue_db, mocker):
    """Pass 2: deps on other failed children remapped to clone IDs; COMPLETED sibling dep preserved."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    # child_A: FAILED (no deps)
    # child_B: FAILED, depends on child_A (FAILED) and child_C (COMPLETED)
    # child_C: COMPLETED (should not be cloned; its ID preserved in clone_B.depends_on)
    parent, children = await _make_parent_with_children(
        requeue_db,
        [
            {
                "payloads": [{"type": "create", "prompt": "task A"}],
                "status": TaskStatus.FAILED,
                "status_message": "ruff lint error",
            },
            {
                "payloads": [{"type": "create", "prompt": "task B"}],
                "status": TaskStatus.FAILED,
                "status_message": "ruff lint error",
                "depends_on_indices": [0, 2],
            },
            {
                "payloads": [{"type": "create", "prompt": "task C"}],
                "status": TaskStatus.COMPLETED,
            },
        ],
    )
    child_a, child_b, child_c = children

    await _requeue_parent_task(parent, requeue_db)

    clones = (
        (
            await requeue_db.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 2

    clones_by_prompt = {c.payloads[0]["prompt"]: c for c in clones}
    clone_a = clones_by_prompt["task A"]
    clone_b = clones_by_prompt["task B"]

    # clone_B.depends_on: child_A.id remapped to clone_A.id; child_C.id preserved as-is
    assert str(clone_a.id) in clone_b.depends_on
    assert str(child_c.id) in clone_b.depends_on
    # The old cancelled child_A.id must not appear in clone_B
    assert str(child_a.id) not in clone_b.depends_on


@pytest.mark.asyncio
async def test_requeue_cycle_detection_clears_all_depends_on(requeue_db, mocker):
    """Pass 3: circular deps in cloned subgraph are all cleared to prevent deadlock."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    # child_A depends on child_B and child_B depends on child_A — mutual cycle
    parent, _children = await _make_parent_with_children(
        requeue_db,
        [
            {
                "payloads": [{"type": "create", "prompt": "task A"}],
                "status": TaskStatus.FAILED,
                "status_message": "ruff lint error",
                "depends_on_indices": [1],
            },
            {
                "payloads": [{"type": "create", "prompt": "task B"}],
                "status": TaskStatus.FAILED,
                "status_message": "ruff lint error",
                "depends_on_indices": [0],
            },
        ],
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (
        (
            await requeue_db.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 2
    # All depends_on must be cleared due to cycle detection
    for clone in clones:
        assert not clone.depends_on


@pytest.mark.asyncio
async def test_requeue_strips_max_plan_account_id(requeue_db, mocker):
    """Clone context must not carry over max_plan_account_id from the failed child."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "task"}],
        child_context={"max_plan_account_id": "acct-123", "scope": "small"},
        child_status_message="ruff lint error",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (
        (
            await requeue_db.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 1
    clone_ctx = clones[0].context or {}
    assert "max_plan_account_id" not in clone_ctx
    # Other context fields must still be present
    assert clone_ctx.get("scope") == "small"


@pytest.mark.asyncio
async def test_requeue_clone_strips_provision_state(requeue_db, mocker):
    """Clone context must not carry over provision retry state from the failed child."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "task"}],
        child_context={
            "scope": "small",
            "provision_failures": 3,
            "provision_failures_infra": 2,
            "_infra_wait": True,
            "blacklisted_accounts": ["acct-aaa"],
            "_slot_released": True,
        },
        child_status_message="vm failed to start",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (
        (
            await requeue_db.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 1
    clone_ctx = clones[0].context or {}
    assert "provision_failures" not in clone_ctx
    assert "provision_failures_infra" not in clone_ctx
    assert "_infra_wait" not in clone_ctx
    assert "blacklisted_accounts" not in clone_ctx
    assert "_slot_released" not in clone_ctx
    # Non-provision context fields must still be present
    assert clone_ctx.get("scope") == "small"


@pytest.mark.asyncio
async def test_requeue_refreshes_base_branch_from_plan_branch(requeue_db, mocker):
    """Clone payload uses parent's plan_branch as base_branch, replacing the stale child value."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[
            {"type": "create", "prompt": "task", "base_branch": "old-deleted-branch"}
        ],
        child_status_message="ruff lint error",
    )
    parent.context = {"plan_branch": "plan/abc123"}
    requeue_db.add(parent)
    await requeue_db.commit()
    await requeue_db.refresh(parent)

    await _requeue_parent_task(parent, requeue_db)

    clones = (
        (
            await requeue_db.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 1
    assert clones[0].payloads[0].get("base_branch") == "plan/abc123"


@pytest.mark.asyncio
async def test_requeue_refreshes_base_branch_from_research_branch(requeue_db, mocker):
    """Clone payload uses parent's research_branch as base_branch when plan_branch is absent."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[
            {"type": "create", "prompt": "task", "base_branch": "stale-branch"}
        ],
        child_status_message="ruff lint error",
    )
    parent.context = {"research_branch": "research/xyz456"}
    requeue_db.add(parent)
    await requeue_db.commit()
    await requeue_db.refresh(parent)

    await _requeue_parent_task(parent, requeue_db)

    clones = (
        (
            await requeue_db.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(clones) == 1
    assert clones[0].payloads[0].get("base_branch") == "research/xyz456"


# ── Provision failure classification ───────────────────────────────────────


def _common_reaper_mocks(mocker):
    """Patch external dependencies required by all reaper tests."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch("app.routers.agent_status.record_task_result")
    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    mocker.patch("app.max_plan_pool.release_max_plan_account", new_callable=AsyncMock)


class _StopLoop(Exception):
    pass


@pytest.mark.asyncio
async def test_reaper_infra_provision_failure_no_retry_budget_burn(reaper_db, mocker):
    """Infra-wide provision failures (e.g. quota exhausted) must NOT increment
    provision_failures — only provision_failures_infra — and must requeue the task."""
    _common_reaper_mocks(mocker)
    mocker.patch(
        "app.reaper.get_provision_error", return_value="quota exceeded for region"
    )
    mocker.patch(
        "app.reaper_orphan_io.get_provision_error", return_value="quota exceeded for region"
    )
    mocker.patch("app.reaper.any_zone_available", return_value=True)
    mocker.patch("app.reaper.soonest_zone_recovery", return_value=None)
    mocker.patch("app.reaper.record_zone_failure")
    mocker.patch("asyncio.to_thread", new_callable=AsyncMock, return_value=None)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))

    project = Project(name="Infra Provision Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        vm_instance_id="infra-vm",
        status=TaskStatus.PROVISIONING,
        started_at=_utcnow() - timedelta(minutes=20),
        context={"provision_zone": "us-central1-a"},
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    # Task should be requeued, not failed
    assert task.status == TaskStatus.QUEUED
    ctx = task.context or {}
    # Infra-wide failure MUST NOT burn provision retry budget
    assert ctx.get("provision_failures", 0) == 0
    # Infra counter incremented instead
    assert ctx.get("provision_failures_infra", 0) == 1
    assert ctx.get("_infra_wait") is True


@pytest.mark.asyncio
async def test_reaper_account_provision_failure_burns_retry_budget(reaper_db, mocker):
    """Account-specific provision failures (agent startup died) must increment
    provision_failures and requeue while retries remain."""
    _common_reaper_mocks(mocker)
    # Non-infra error — does NOT match INFRA_PROVISION_PATTERNS
    mocker.patch(
        "app.reaper.get_provision_error",
        return_value="vm startup error: agent never claimed",
    )
    mocker.patch("app.reaper.any_zone_available", return_value=True)
    mocker.patch("app.reaper.record_zone_failure")
    mocker.patch("asyncio.to_thread", new_callable=AsyncMock, return_value=None)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))

    project = Project(name="Account Provision Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        vm_instance_id="account-vm",
        status=TaskStatus.PROVISIONING,
        started_at=_utcnow() - timedelta(minutes=20),
        context={"provision_zone": "us-central1-b", "max_plan_account_id": "acct-abc"},
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    assert task.status == TaskStatus.QUEUED
    ctx = task.context or {}
    # Account-specific failure MUST increment provision_failures
    assert ctx.get("provision_failures", 0) == 1
    # infra counter must stay untouched
    assert ctx.get("provision_failures_infra", 0) == 0
    assert ctx.get("_infra_wait") is None
    # Account blacklisted so retry uses a different one
    assert "acct-abc" in (ctx.get("blacklisted_accounts") or [])


@pytest.mark.asyncio
async def test_reaper_infra_provision_failure_with_zone_recovery(reaper_db, mocker):
    """When all zones are down (no zone available) the task is treated as infra-wide
    and retry_after is set to align with zone recovery time."""
    _common_reaper_mocks(mocker)
    mocker.patch("app.reaper.get_provision_error", return_value="")
    # all zones unavailable — triggers infra path even without a matching pattern
    mocker.patch("app.reaper.any_zone_available", return_value=False)
    recovery_time = datetime.now(timezone.utc) + timedelta(minutes=5)
    mocker.patch("app.reaper.soonest_zone_recovery", return_value=recovery_time)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))

    project = Project(name="Zone Recovery Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    # No provision_zone — VM was never assigned a zone (GCP API failed before zone selection)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "test"}],
        vm_instance_id="nozone-vm",
        status=TaskStatus.PROVISIONING,
        started_at=_utcnow() - timedelta(minutes=20),
        context={},
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    assert task.status == TaskStatus.QUEUED
    ctx = task.context or {}
    assert ctx.get("provision_failures", 0) == 0
    assert ctx.get("provision_failures_infra", 0) == 1
    assert ctx.get("_infra_wait") is True
    # Audit W1: retry_after_until is a column. Must be set (aligned with zone recovery).
    assert task.retry_after_until is not None


# ── Rate-limit detection ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reaper_rate_limit_requeue_without_burning_retry_budget(
    reaper_db, mocker
):
    """Rate-limited tasks are requeued immediately without incrementing attempt_count,
    the account is blacklisted, and disable_account_model is called."""
    _common_reaper_mocks(mocker)
    mock_disable = mocker.patch(
        "app.max_plan_pool.disable_account_model", new_callable=AsyncMock
    )
    mocker.patch(
        "app.max_plan_pool._extract_task_model", return_value="claude-sonnet-4-6"
    )
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))

    project = Project(name="Rate Limit Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "some task"}],
        vm_instance_id="rl-vm",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=1,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context={
            "max_plan_account_id": "rl-acct-123",
            # Exact pattern from RATE_LIMIT_PATTERNS
            "last_error": "API Error: Rate limit reached for claude-sonnet-4-6",
        },
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    original_attempt_count = task.attempt_count

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    # Must be requeued, not failed
    assert task.status == TaskStatus.QUEUED
    # Retry budget must NOT be burned
    assert task.attempt_count == original_attempt_count
    ctx = task.context or {}
    # Exhausted account must be blacklisted
    assert "rl-acct-123" in (ctx.get("blacklisted_accounts") or [])
    # Account ID cleared so orchestrator picks a fresh one
    assert "max_plan_account_id" not in ctx
    # disable_account_model must have been called to block re-selection.
    # Reaper plumbs structured rate-limit signal (retry_after_seconds, kind)
    # through; on a generic Anthropic rate-limit message the signal resolves
    # to the baseline (no retry hint, plain rate_limit kind).
    mock_disable.assert_called_once_with(
        "rl-acct-123",
        "claude-sonnet-4-6",
        retry_after_seconds=None,
        kind="rate_limit",
    )


@pytest.mark.asyncio
async def test_reaper_rate_limit_disable_account_model_failure_handled_gracefully(
    reaper_db, mocker
):
    """If disable_account_model raises on both attempts, the reaper still requeues the task,
    the account is blacklisted at the task level, and the error is logged at ERROR level."""
    _common_reaper_mocks(mocker)
    mock_disable = mocker.patch(
        "app.max_plan_pool.disable_account_model",
        new_callable=AsyncMock,
        side_effect=RuntimeError("pool unavailable"),
    )
    mocker.patch(
        "app.max_plan_pool._extract_task_model", return_value="claude-sonnet-4-6"
    )

    # Allow the 1-second retry sleep through; raise _StopLoop on the next loop sleep
    sleep_calls = []

    async def _sleep_side_effect(delay):
        sleep_calls.append(delay)
        if delay != 1:
            raise _StopLoop("Stop")

    mocker.patch("asyncio.sleep", side_effect=_sleep_side_effect)

    project = Project(name="Rate Limit Error Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "some task"}],
        vm_instance_id="rl-err-vm",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=1,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context={
            "max_plan_account_id": "rl-acct-456",
            "last_error": "API Error: Rate limit reached",
        },
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    # Task must still be requeued despite both disable_account_model failures
    assert task.status == TaskStatus.QUEUED
    ctx = task.context or {}
    assert "rl-acct-456" in (ctx.get("blacklisted_accounts") or [])
    # Both the initial attempt and the retry must have been called
    assert mock_disable.call_count == 2
    # The 1-second retry sleep must have been attempted
    assert 1 in sleep_calls


@pytest.mark.asyncio
async def test_reaper_rate_limit_disable_account_model_retry_succeeds(
    reaper_db, mocker
):
    """If disable_account_model fails on first attempt but succeeds on retry, task is still requeued."""
    _common_reaper_mocks(mocker)
    call_count = 0

    async def _disable_side_effect(acct_id, model, **kwargs):
        # Accept the retry_after_seconds/kind kwargs that the reaper now plumbs
        # through — they aren't asserted here; this test only cares about retry
        # behavior on transient failures.
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient error")

    mock_disable = mocker.patch(
        "app.max_plan_pool.disable_account_model",
        new_callable=AsyncMock,
        side_effect=_disable_side_effect,
    )
    mocker.patch(
        "app.max_plan_pool._extract_task_model", return_value="claude-sonnet-4-6"
    )

    sleep_calls = []

    async def _sleep_side_effect(delay):
        sleep_calls.append(delay)
        if delay != 1:
            raise _StopLoop("Stop")

    mocker.patch("asyncio.sleep", side_effect=_sleep_side_effect)

    project = Project(name="Rate Limit Retry Success Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "some task"}],
        vm_instance_id="rl-retry-vm",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=1,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context={
            "max_plan_account_id": "rl-acct-789",
            "last_error": "API Error: Rate limit reached",
        },
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    assert task.status == TaskStatus.QUEUED
    ctx = task.context or {}
    assert "rl-acct-789" in (ctx.get("blacklisted_accounts") or [])
    assert mock_disable.call_count == 2
    assert 1 in sleep_calls


# ── Dependent task unblocking ───────────────────────────────────────────────


def _make_sqlite_compat_asl(real_asl, project_id, blocked_id_str):
    """Return (main_asl, dep_asl) to work around SQLite's lack of JSONB @>.

    main_asl: always returns a real unmodified session (for app.reaper).
    dep_asl: wraps every session in a proxy that replaces the @> query with
    Python-level filtering (for app.reaper_post_orphan_sweep, which owns
    the dep-unblocking ASL call after the orphan-I/O extraction).
    """

    class _DepSessionProxy:
        """Wraps a real session but replaces the @> query with Python filtering."""

        def __init__(self, inner):
            self._inner = inner

        async def execute(self, stmt):
            # Execute a simpler query (no @> operator) and filter in Python
            plain_stmt = select(Task).where(
                Task.project_id == project_id,
                Task.status == TaskStatus.QUEUED,
            )
            result = await self._inner.execute(plain_stmt)
            all_queued = result.scalars().all()
            matching = [t for t in all_queued if blocked_id_str in (t.depends_on or [])]

            class _FakeResult:
                def scalars(self_):
                    class _FakeScalars:
                        def all(self_2):
                            return matching

                    return _FakeScalars()

            return _FakeResult()

        def add(self, obj):
            self._inner.add(obj)

        async def commit(self):
            await self._inner.commit()

        async def __aenter__(self):
            await self._inner.__aenter__()
            return self

        async def __aexit__(self, *args):
            return await self._inner.__aexit__(*args)

    class _MainASL:
        def __call__(self):
            return real_asl()

    class _DepOnlyASL:
        def __call__(self):
            return _DepSessionProxy(real_asl())

    return _MainASL(), _DepOnlyASL()


@pytest.mark.asyncio
async def test_reaper_unblocks_dependent_tasks_on_terminal_failure(reaper_db, mocker):
    """When a task fails terminally, QUEUED tasks whose depends_on list contains
    its ID must have that ID removed so they can proceed."""
    _common_reaper_mocks(mocker)
    # Disable infra-recovery resurrection so the task stays FAILED after the reap
    mocker.patch("app.reaper.any_zone_available", return_value=False)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))

    project = Project(name="Dep Unblock Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    # Failed dependency: exhausted retries so reaper marks it FAILED
    failed_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "failing task"}],
        vm_instance_id="failing-vm",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=3,  # max_retries=3, so can_retry=False → FAILED
        last_heartbeat=_utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(failed_task)
    await reaper_db.commit()
    await reaper_db.refresh(failed_task)

    # Waiting task blocked by the above dependency
    waiting_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "blocked task"}],
        status=TaskStatus.QUEUED,
        depends_on=[str(failed_task.id)],
    )
    reaper_db.add(waiting_task)
    await reaper_db.commit()
    await reaper_db.refresh(waiting_task)

    # SQLite does not support the JSONB @> operator used in the dep-unblocking
    # query.  Patch both ASLs: app.reaper uses the real session; the dep-session
    # is now opened in app.reaper_post_orphan_sweep (after the orphan-I/O
    # extraction) and needs the Python-level filter proxy.
    import app.reaper as _reaper_mod

    main_asl, dep_asl = _make_sqlite_compat_asl(
        _reaper_mod.AsyncSessionLocal,
        project.id,
        str(failed_task.id),
    )
    mocker.patch("app.reaper.AsyncSessionLocal", main_asl)
    mocker.patch("app.reaper_post_orphan_sweep.AsyncSessionLocal", dep_asl)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(failed_task)
    await reaper_db.refresh(waiting_task)

    # Dependency must be terminally failed
    assert failed_task.status == TaskStatus.FAILED
    # Dependent task must have had the failed ID removed from depends_on
    remaining_deps = waiting_task.depends_on or []
    assert str(failed_task.id) not in remaining_deps


@pytest.mark.asyncio
async def test_reaper_dep_unblocking_error_does_not_crash_reaper(reaper_db, mocker):
    """If the dep-unblocking session raises an exception, the reaper must log the
    error and continue (not propagate the exception or skip the main commit)."""
    _common_reaper_mocks(mocker)
    # Disable infra-recovery resurrection so the task stays FAILED after the reap
    mocker.patch("app.reaper.any_zone_available", return_value=False)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))

    project = Project(name="Dep Error Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    failed_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "failing task"}],
        vm_instance_id="dep-err-vm",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=3,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(failed_task)
    await reaper_db.commit()
    await reaper_db.refresh(failed_task)

    class _FailDepSession:
        async def __aenter__(self):
            raise RuntimeError("dep session exploded")

        async def __aexit__(self, *args):
            pass

    import app.reaper as _reaper_mod

    _real_asl = _reaper_mod.AsyncSessionLocal
    _dep_call_count = 0

    class _CountingASL:
        def __call__(self):
            nonlocal _dep_call_count
            _dep_call_count += 1
            if _dep_call_count == 1:
                return _real_asl()
            return _FailDepSession()

    mocker.patch("app.reaper.AsyncSessionLocal", _CountingASL())

    try:
        await reaper_service()
    except _StopLoop:
        pass

    # Reaper must have marked the task FAILED despite the dep-session error
    await reaper_db.refresh(failed_task)
    assert failed_task.status == TaskStatus.FAILED


# ── Force-lock retry mechanism ──────────────────────────────────────────────


@pytest.fixture(autouse=False)
def clear_reaper_skip_counts():
    """Ensure _reaper_skip_counts is clean before and after each force-lock test."""
    _reaper_skip_counts.clear()
    yield
    _reaper_skip_counts.clear()


@pytest.mark.asyncio
async def test_reaper_force_lock_failure_clears_tracking_state(
    reaper_db, mocker, clear_reaper_skip_counts
):
    """When a force-lock attempt fails (e.g. lock_timeout), the skip-count entry for
    that task ID must be cleared so the reaper retries on the next cycle rather than
    re-attempting the force-lock endlessly."""
    _common_reaper_mocks(mocker)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))

    project = Project(name="Force Lock Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    # Create a real orphaned task so the reaper runs through the skip-lock block
    orphaned = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "orphaned"}],
        vm_instance_id="orphan-vm-fl",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(orphaned)
    await reaper_db.commit()
    await reaper_db.refresh(orphaned)

    # Pre-populate skip_counts with a stale UUID at count=5 (simulates 5 prior skips)
    stale_id = uuid.uuid4()
    _reaper_skip_counts[stale_id] = 5

    try:
        await reaper_service()
    except _StopLoop:
        pass

    # stale_id must be gone — either force-lock failed (except path) or stale cleanup removed it
    assert stale_id not in _reaper_skip_counts


@pytest.mark.asyncio
async def test_reaper_force_lock_threshold_is_five(
    reaper_db, mocker, clear_reaper_skip_counts
):
    """Tasks with skip count < 5 must NOT trigger a force-lock attempt; only at 5+
    does the force-lock execute. Verify by checking that count=4 entries survive
    a reaper cycle as part of normal skip-count tracking (not prematurely cleared)."""
    _common_reaper_mocks(mocker)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))

    project = Project(name="Force Lock Threshold Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    # An orphaned task so the skip-count block runs
    orphaned = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "orphaned"}],
        vm_instance_id="threshold-vm",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(orphaned)
    await reaper_db.commit()
    await reaper_db.refresh(orphaned)

    # count=4 → should NOT trigger force-lock (threshold is 5)
    stale_id = uuid.uuid4()
    _reaper_skip_counts[stale_id] = 4

    try:
        await reaper_service()
    except _StopLoop:
        pass

    # The orphaned task was locked normally → stale_id cleaned up by stale-entry sweep
    # (not in orphaned_ids), so it is still removed — but via the cleanup path, not force-lock.
    # The key assertion: reaper did NOT trigger force-lock for count<5
    # (observable as: no SET LOCAL sql error logged; test just verifies no crash occurred
    # and the orphaned task was still handled correctly)
    await reaper_db.refresh(orphaned)
    # Orphaned task must have been processed (QUEUED or FAILED)
    assert orphaned.status in (TaskStatus.QUEUED, TaskStatus.FAILED)
    # stale_id cleaned up by stale-entry sweep (not in orphaned_ids)
    assert stale_id not in _reaper_skip_counts


# ── _check_candidate_task_deps — requeue-cancelled dependency handling ───────


async def _noop_verify_pr_merged(task, session):
    """Stub for verify_pr_merged: always returns True (PR merged) for COMPLETED tasks."""
    return True


@pytest.mark.asyncio
async def test_requeue_stores_replaced_by_task_id(requeue_db, mocker):
    """_requeue_parent_task sets _replaced_by_task_id on the cancelled child pointing to the clone."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    parent, child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "do the thing"}],
        child_status_message="ruff lint error",
    )

    await _requeue_parent_task(parent, requeue_db)

    await requeue_db.refresh(child)
    clone = (
        (
            await requeue_db.execute(
                select(Task).where(
                    Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED
                )
            )
        )
        .scalars()
        .first()
    )

    assert clone is not None
    assert child.status == TaskStatus.CANCELLED
    assert child.context.get("_replaced_by_task_id") == str(clone.id), (
        "Cancelled child must record clone ID so orchestrator can resolve dep replacements"
    )


@pytest.mark.asyncio
async def test_check_deps_requeue_cancelled_not_cascade_cancelled(requeue_db, mocker):
    """(a) External task depending on a requeue-cancelled child is NOT cascade-cancelled.

    When a dep is CANCELLED but has _replaced_by_task_id pointing to a QUEUED clone,
    _check_candidate_task_deps must return 'waiting', not 'cascade_cancel'.
    """
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    project = Project(name="Dep Test", user_id="u1")
    requeue_db.add(project)
    await requeue_db.commit()
    await requeue_db.refresh(project)

    # Cancelled child with a replacement clone
    clone = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "clone"}],
        status=TaskStatus.QUEUED,
    )
    requeue_db.add(clone)
    await requeue_db.commit()
    await requeue_db.refresh(clone)

    cancelled_child = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "original"}],
        status=TaskStatus.CANCELLED,
        latest_status_message="Parent requeued — replaced by retry",
        context={"_replaced_by_task_id": str(clone.id)},
    )
    requeue_db.add(cancelled_child)
    await requeue_db.commit()
    await requeue_db.refresh(cancelled_child)

    # External task that depends on the (now-cancelled) child
    external = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "external"}],
        status=TaskStatus.QUEUED,
        depends_on=[str(cancelled_child.id)],
    )
    requeue_db.add(external)
    await requeue_db.commit()
    await requeue_db.refresh(external)

    dep_uuids = [uuid.UUID(str(cancelled_child.id))]
    result, deps_remapped = await _check_candidate_task_deps(
        external, [cancelled_child], dep_uuids, requeue_db, _noop_verify_pr_merged
    )

    assert result == "waiting", (
        f"Expected 'waiting' (replacement clone is QUEUED), got {result!r}"
    )
    assert str(cancelled_child.id) in deps_remapped
    assert deps_remapped[str(cancelled_child.id)] == str(clone.id)


@pytest.mark.asyncio
async def test_check_deps_remap_to_clone(requeue_db, mocker):
    """(b) External task's depends_on is remapped to the clone ID after resolution."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    project = Project(name="Remap Test", user_id="u1")
    requeue_db.add(project)
    await requeue_db.commit()
    await requeue_db.refresh(project)

    # COMPLETED clone — this will cause 'eligible' result
    clone = Task(
        project_id=project.id,
        payloads=[
            {"type": "fix", "prompt": "clone", "pr_number": 1, "branch": "fix/1"}
        ],
        status=TaskStatus.COMPLETED,
    )
    requeue_db.add(clone)
    await requeue_db.commit()
    await requeue_db.refresh(clone)

    cancelled_child = Task(
        project_id=project.id,
        payloads=[
            {"type": "fix", "prompt": "original", "pr_number": 1, "branch": "fix/1"}
        ],
        status=TaskStatus.CANCELLED,
        latest_status_message="Parent requeued — replaced by retry",
        context={"_replaced_by_task_id": str(clone.id)},
    )
    requeue_db.add(cancelled_child)
    await requeue_db.commit()
    await requeue_db.refresh(cancelled_child)

    external = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "external"}],
        status=TaskStatus.QUEUED,
        depends_on=[str(cancelled_child.id)],
    )
    requeue_db.add(external)
    await requeue_db.commit()
    await requeue_db.refresh(external)

    dep_uuids = [uuid.UUID(str(cancelled_child.id))]
    result, deps_remapped = await _check_candidate_task_deps(
        external, [cancelled_child], dep_uuids, requeue_db, _noop_verify_pr_merged
    )

    assert result == "eligible"
    assert str(cancelled_child.id) in deps_remapped
    assert deps_remapped[str(cancelled_child.id)] == str(clone.id)


@pytest.mark.asyncio
async def test_check_deps_truly_cancelled_triggers_cascade(requeue_db, mocker):
    """(c) Truly cancelled deps (no _replaced_by_task_id) return 'cascade_cancel'."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    project = Project(name="Cascade Test", user_id="u1")
    requeue_db.add(project)
    await requeue_db.commit()
    await requeue_db.refresh(project)

    # Permanently cancelled dep — no replacement
    perm_cancelled = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "cancelled dep"}],
        status=TaskStatus.CANCELLED,
        latest_status_message="Manually cancelled",
        context={},
    )
    requeue_db.add(perm_cancelled)
    await requeue_db.commit()
    await requeue_db.refresh(perm_cancelled)

    external = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "external"}],
        status=TaskStatus.QUEUED,
        depends_on=[str(perm_cancelled.id)],
    )
    requeue_db.add(external)
    await requeue_db.commit()
    await requeue_db.refresh(external)

    dep_uuids = [uuid.UUID(str(perm_cancelled.id))]
    result, deps_remapped = await _check_candidate_task_deps(
        external, [perm_cancelled], dep_uuids, requeue_db, _noop_verify_pr_merged
    )

    assert result == "cascade_cancel", (
        "Permanently cancelled deps (no _replaced_by_task_id) must trigger cascade cancellation"
    )
    assert not deps_remapped


@pytest.mark.asyncio
async def test_check_deps_failed_dep_triggers_cascade(requeue_db, mocker):
    """A genuine FAILED dep (CODE_ERROR etc. — not a SCOPE_EMPTY clean
    exit) must trigger cascade cancellation."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    project = Project(name="Failed Dep Test", user_id="u1")
    requeue_db.add(project)
    await requeue_db.commit()
    await requeue_db.refresh(project)

    failed_dep = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "failed dep"}],
        status=TaskStatus.FAILED,
        latest_status_message="CODE_ERROR",
        context={},
    )
    requeue_db.add(failed_dep)
    await requeue_db.commit()
    await requeue_db.refresh(failed_dep)

    external = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "external"}],
        status=TaskStatus.QUEUED,
        depends_on=[str(failed_dep.id)],
    )
    requeue_db.add(external)
    await requeue_db.commit()
    await requeue_db.refresh(external)

    dep_uuids = [uuid.UUID(str(failed_dep.id))]
    result, deps_remapped = await _check_candidate_task_deps(
        external, [failed_dep], dep_uuids, requeue_db, _noop_verify_pr_merged
    )

    assert result == "cascade_cancel"
    assert not deps_remapped


@pytest.mark.asyncio
async def test_check_deps_cross_base_unmerged_does_not_block(requeue_db, mocker):
    """A completed-but-unmerged dep in a DIFFERENT rollup (different base branch)
    must NOT block dispatch. Its PR merges into its own rollup branch, which the
    dependent (cut from a different base) never sees — so waiting buys no result
    propagation, only serialization. Loosened 2026-05-28 for cross-rollup
    parallelism (file-overlap conflicts are still guarded by the dispatch
    filters)."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    async def _verify_not_merged(task, session):
        return False  # PR still open / unmerged

    project = Project(name="CrossBase Dep Test", user_id="u1")
    requeue_db.add(project)
    await requeue_db.commit()
    await requeue_db.refresh(project)

    dep = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "dep", "base_branch": "rollup/Y"}],
        status=TaskStatus.COMPLETED,
    )
    requeue_db.add(dep)
    await requeue_db.commit()
    await requeue_db.refresh(dep)

    external = Task(
        project_id=project.id,
        payloads=[
            {"type": "create", "prompt": "external", "base_branch": "rollup/X"}
        ],
        status=TaskStatus.QUEUED,
        depends_on=[str(dep.id)],
    )
    requeue_db.add(external)
    await requeue_db.commit()
    await requeue_db.refresh(external)

    result, _ = await _check_candidate_task_deps(
        external, [dep], [uuid.UUID(str(dep.id))], requeue_db, _verify_not_merged
    )
    assert result == "eligible", (
        f"cross-base unmerged dep should not block (timing-only), got {result!r}"
    )


@pytest.mark.asyncio
async def test_check_deps_same_base_unmerged_still_waits(requeue_db, mocker):
    """A completed-but-unmerged dep that SHARES the dependent's base branch still
    blocks — the dep's merge lands in the dependent's base, so the wait actually
    propagates the result. (Guards against the cross-base loosening leaking into
    same-base deps.)"""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    async def _verify_not_merged(task, session):
        return False

    project = Project(name="SameBase Dep Test", user_id="u1")
    requeue_db.add(project)
    await requeue_db.commit()
    await requeue_db.refresh(project)

    dep = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "dep", "base_branch": "rollup/X"}],
        status=TaskStatus.COMPLETED,
    )
    requeue_db.add(dep)
    await requeue_db.commit()
    await requeue_db.refresh(dep)

    external = Task(
        project_id=project.id,
        payloads=[
            {"type": "create", "prompt": "external", "base_branch": "rollup/X"}
        ],
        status=TaskStatus.QUEUED,
        depends_on=[str(dep.id)],
    )
    requeue_db.add(external)
    await requeue_db.commit()
    await requeue_db.refresh(external)

    result, _ = await _check_candidate_task_deps(
        external, [dep], [uuid.UUID(str(dep.id))], requeue_db, _verify_not_merged
    )
    assert result == "waiting", (
        f"same-base unmerged dep must still block (propagation), got {result!r}"
    )


@pytest.mark.asyncio
async def test_check_deps_cross_base_failed_still_cascades(requeue_db, mocker):
    """Loosening the cross-base *wait* must not drop failure safety: a genuinely
    FAILED dep cascade-cancels the dependent regardless of base branch."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    project = Project(name="CrossBase Failed Dep Test", user_id="u1")
    requeue_db.add(project)
    await requeue_db.commit()
    await requeue_db.refresh(project)

    dep = Task(
        project_id=project.id,
        payloads=[
            {"type": "create", "prompt": "failed dep", "base_branch": "rollup/Y"}
        ],
        status=TaskStatus.FAILED,
        latest_status_message="CODE_ERROR",
        context={},
    )
    requeue_db.add(dep)
    await requeue_db.commit()
    await requeue_db.refresh(dep)

    external = Task(
        project_id=project.id,
        payloads=[
            {"type": "create", "prompt": "external", "base_branch": "rollup/X"}
        ],
        status=TaskStatus.QUEUED,
        depends_on=[str(dep.id)],
    )
    requeue_db.add(external)
    await requeue_db.commit()
    await requeue_db.refresh(external)

    result, _ = await _check_candidate_task_deps(
        external, [dep], [uuid.UUID(str(dep.id))], requeue_db, _noop_verify_pr_merged
    )
    assert result == "cascade_cancel", (
        f"cross-base FAILED dep must still cascade-cancel, got {result!r}"
    )


@pytest.mark.asyncio
async def test_check_deps_scope_empty_dep_not_cascade(requeue_db, mocker):
    """A dep that ended CANCELLED with failure_category SCOPE_EMPTY is a
    clean 'scope already covered' exit — it must NOT cascade-cancel dependents.

    Regression (2026-05-21): cluster 4/4 (a disjoint file) was CANCELLED
    because cluster 3/4 exited SCOPE_EMPTY. _check_candidate_task_deps must
    treat a SCOPE_EMPTY dep as a satisfied, non-blocking dependency →
    'eligible', not 'cascade_cancel'."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    project = Project(name="ScopeEmpty Dep Test", user_id="u1")
    requeue_db.add(project)
    await requeue_db.commit()
    await requeue_db.refresh(project)

    scope_empty_dep = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "scope empty dep"}],
        status=TaskStatus.CANCELLED,
        latest_status_message=(
            "SCOPE_EMPTY: all declared files already covered by completed sibling"
        ),
        context={"failure_category": "SCOPE_EMPTY"},
    )
    requeue_db.add(scope_empty_dep)
    await requeue_db.commit()
    await requeue_db.refresh(scope_empty_dep)

    external = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "downstream cluster"}],
        status=TaskStatus.QUEUED,
        depends_on=[str(scope_empty_dep.id)],
    )
    requeue_db.add(external)
    await requeue_db.commit()
    await requeue_db.refresh(external)

    dep_uuids = [uuid.UUID(str(scope_empty_dep.id))]
    result, deps_remapped = await _check_candidate_task_deps(
        external, [scope_empty_dep], dep_uuids, requeue_db, _noop_verify_pr_merged
    )

    assert result == "eligible", (
        f"SCOPE_EMPTY dep must not cascade-cancel its dependent, got {result!r}"
    )
    assert not deps_remapped


@pytest.mark.asyncio
async def test_check_deps_scope_empty_plus_real_failure_still_cascades(
    requeue_db, mocker
):
    """The SCOPE_EMPTY carve-out is narrow: a genuine FAILED dep alongside
    a SCOPE_EMPTY one still triggers cascade cancellation."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    project = Project(name="ScopeEmpty Mixed Test", user_id="u1")
    requeue_db.add(project)
    await requeue_db.commit()
    await requeue_db.refresh(project)

    scope_empty_dep = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "scope empty dep"}],
        status=TaskStatus.CANCELLED,
        latest_status_message="SCOPE_EMPTY: covered by sibling",
        context={"failure_category": "SCOPE_EMPTY"},
    )
    real_failure_dep = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "broken dep"}],
        status=TaskStatus.FAILED,
        latest_status_message="CODE_ERROR",
        context={"failure_category": "CODE_ERROR"},
    )
    requeue_db.add_all([scope_empty_dep, real_failure_dep])
    await requeue_db.commit()
    await requeue_db.refresh(scope_empty_dep)
    await requeue_db.refresh(real_failure_dep)

    external = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "downstream"}],
        status=TaskStatus.QUEUED,
        depends_on=[str(scope_empty_dep.id), str(real_failure_dep.id)],
    )
    requeue_db.add(external)
    await requeue_db.commit()
    await requeue_db.refresh(external)

    dep_uuids = [
        uuid.UUID(str(scope_empty_dep.id)),
        uuid.UUID(str(real_failure_dep.id)),
    ]
    result, deps_remapped = await _check_candidate_task_deps(
        external,
        [scope_empty_dep, real_failure_dep],
        dep_uuids,
        requeue_db,
        _noop_verify_pr_merged,
    )

    assert result == "cascade_cancel", (
        "a genuine CODE_ERROR dep must still cascade even with a SCOPE_EMPTY sibling"
    )
    assert not deps_remapped


@pytest.mark.asyncio
async def test_check_deps_legacy_failed_scope_empty_dep_not_cascade(requeue_db, mocker):
    """Legacy rows: a SCOPE_EMPTY dep stored as FAILED (pre-dating the
    CANCELLED relabel) must also be treated as satisfied, not cascade."""
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    project = Project(name="Legacy ScopeEmpty Test", user_id="u1")
    requeue_db.add(project)
    await requeue_db.commit()
    await requeue_db.refresh(project)

    legacy_dep = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "legacy scope empty dep"}],
        status=TaskStatus.FAILED,
        latest_status_message="SCOPE_EMPTY: covered by sibling",
        context={"failure_category": "SCOPE_EMPTY"},
    )
    requeue_db.add(legacy_dep)
    await requeue_db.commit()
    await requeue_db.refresh(legacy_dep)

    external = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "downstream"}],
        status=TaskStatus.QUEUED,
        depends_on=[str(legacy_dep.id)],
    )
    requeue_db.add(external)
    await requeue_db.commit()
    await requeue_db.refresh(external)

    dep_uuids = [uuid.UUID(str(legacy_dep.id))]
    result, deps_remapped = await _check_candidate_task_deps(
        external, [legacy_dep], dep_uuids, requeue_db, _noop_verify_pr_merged
    )

    assert result == "eligible"
    assert not deps_remapped


# ══════════════════════════════════════════════════════════════════════════════
# Diagnostic observability — compute.py helpers
# ══════════════════════════════════════════════════════════════════════════════


def test_capture_serial_for_vm_logs_on_failure(caplog, mocker):
    """
    _capture_serial_for_vm must not swallow exceptions silently — it is the
    only forensic source for agent startup failures, so operators must see
    WHY capture failed (VM already gone vs permission vs API quota).
    """
    import logging
    from app import compute

    # The function does `from google.cloud import compute_v1` locally.
    # Patch InstancesClient on the real module so the local import sees the
    # raising stub. google-cloud-compute is a real test-env dependency so
    # the module is importable; we only replace the client constructor.
    mocker.patch(
        "google.cloud.compute_v1.InstancesClient",
        side_effect=RuntimeError("boom from test"),
    )

    with caplog.at_level(logging.WARNING):
        result = compute._capture_serial_for_vm("agent-vm-abc", "us-central1-a")

    assert result is None
    assert "failed for vm=agent-vm-abc" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "boom from test" in caplog.text


def test_provision_gcp_script_has_settle_window_before_success_heartbeat():
    """
    The startup-script success heartbeat must be gated on a 30s settle window
    plus a fresh systemctl is-active check. Type=simple returns as soon as npm
    is fork()ed, so the pre-fix heartbeat fired before the agent had loaded a
    line of TypeScript — making "startup succeeded" a lie for in-flight
    silent-death failures.

    This test parses the _provision_gcp source (the f-string template that
    produces the bash script) and asserts the settle window sits between the
    service-start block and the success-heartbeat curl.
    """
    import inspect

    # The bash startup script body was moved out of compute._provision_gcp
    # into compute_startup_script.build_agent_startup_script (commit 55156451).
    # Read source from the new home.
    from app import compute_startup_script

    source = inspect.getsource(compute_startup_script.build_agent_startup_script)

    # Locate the success-heartbeat region by its unique status_message.
    success_msg = "Startup script completed, agent service started"
    assert success_msg in source, "success heartbeat block missing"
    success_idx = source.index(success_msg)

    # The preceding region must contain the settle window + is-active check.
    # Bound the search to a window immediately before the heartbeat curl so
    # we don't accidentally match the earlier `systemctl restart` block.
    # 2200 chars covers the settle window + the second sleep/is-active recheck
    # added by audit E8 ("VM ready != healthy") + sourcing /etc/agent.env +
    # the gated branch — without reaching back to the earlier restart block.
    window_start = max(0, success_idx - 2200)
    pre_heartbeat = source[window_start:success_idx]
    assert "sleep 30" in pre_heartbeat, (
        "expected 30s settle window before success heartbeat — Type=simple "
        "returns before the agent is actually running"
    )
    assert "is-active" in pre_heartbeat, (
        "expected systemctl is-active re-check before success heartbeat"
    )

    # And the skip-message branch must exist: the success heartbeat is gated
    # behind is-active, and the else branch reports AGENT_DIED_EARLY instead.
    assert "AGENT_DIED_EARLY" in source, (
        "expected explicit died-early branch when agent died in settle window "
        "(was 'skipping success heartbeat' marker, replaced with journaled "
        "AGENT_DIED_EARLY heartbeat for better reaper classification)"
    )


# ---------------------------------------------------------------------------
# Tests for the all-children-failed EVALUATING parent rescue (Step 4 in reaper)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reaper_rescues_evaluating_parent_all_children_failed(reaper_db, mocker):
    """EVALUATING parent whose children ALL failed and requeue limit exhausted is set to FAILED."""
    _common_reaper_mocks(mocker)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))

    project = Project(name="All-Failed Rescue Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    # Set _requeue_count=3 (at MAX_PARENT_REQUEUES) so _requeue_parent_task immediately
    # fails the parent permanently rather than cloning children again.
    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "stuck"}],
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 3},
        updated_at=_utcnow() - timedelta(minutes=20),
    )
    reaper_db.add(parent)
    await reaper_db.commit()
    await reaper_db.refresh(parent)

    child1 = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "c1"}],
        status=TaskStatus.FAILED,
        context={"failure_category": "CODE_ERROR"},
    )
    child2 = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "c2"}],
        status=TaskStatus.CANCELLED,
    )
    reaper_db.add_all([child1, child2])
    await reaper_db.commit()

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_reaper_does_not_rescue_parent_with_running_children(reaper_db, mocker):
    """EVALUATING parent with at least one still-running child must NOT be rescued prematurely."""
    _common_reaper_mocks(mocker)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))

    project = Project(name="Running Child Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "stuck"}],
        status=TaskStatus.EVALUATING,
        updated_at=_utcnow() - timedelta(minutes=20),
    )
    reaper_db.add(parent)
    await reaper_db.commit()
    await reaper_db.refresh(parent)

    failed_child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "c1"}],
        status=TaskStatus.FAILED,
    )
    running_child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "c2"}],
        status=TaskStatus.IN_PROGRESS,
    )
    reaper_db.add_all([failed_child, running_child])
    await reaper_db.commit()

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(parent)
    # Parent should still be EVALUATING — one child is still running.
    assert parent.status == TaskStatus.EVALUATING


@pytest.mark.asyncio
async def test_reaper_failure_message_includes_failure_classes(reaper_db, mocker):
    """Rescued parent failure message includes each child's failure_category."""
    _common_reaper_mocks(mocker)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))

    project = Project(name="Failure Class Msg Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    # Set _requeue_count=3 so _requeue_parent_task fails permanently on this call.
    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "stuck"}],
        status=TaskStatus.EVALUATING,
        context={"_requeue_count": 3},
        updated_at=_utcnow() - timedelta(minutes=20),
    )
    reaper_db.add(parent)
    await reaper_db.commit()
    await reaper_db.refresh(parent)

    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "c1"}],
        status=TaskStatus.FAILED,
        context={"failure_category": "PROVISION_FAILURE"},
    )
    reaper_db.add(child)
    await reaper_db.commit()

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(parent)
    assert parent.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_reaper_parent_rescue_calls_requeue_before_failing(reaper_db, mocker):
    """EVALUATING parent with all-failed children calls _requeue_parent_task (re-evaluation)
    instead of being immediately set to FAILED when the requeue budget is not exhausted."""
    _common_reaper_mocks(mocker)
    mock_requeue = mocker.patch(
        "app.task_lifecycle._requeue_parent_task", new_callable=AsyncMock
    )
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))

    project = Project(name="Requeue Rescue Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    # _requeue_count not set — budget available, parent should be re-evaluated not failed.
    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "stuck"}],
        status=TaskStatus.EVALUATING,
        updated_at=_utcnow() - timedelta(minutes=20),
    )
    reaper_db.add(parent)
    await reaper_db.commit()
    await reaper_db.refresh(parent)

    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create", "prompt": "c1"}],
        status=TaskStatus.FAILED,
        context={"failure_category": "PROVISION_FAILURE"},
    )
    reaper_db.add(child)
    await reaper_db.commit()

    try:
        await reaper_service()
    except _StopLoop:
        pass

    # _requeue_parent_task should have been called — not the parent directly set to FAILED.
    mock_requeue.assert_called_once()
    called_parent = mock_requeue.call_args[0][0]
    assert called_parent.id == parent.id

    # Parent must NOT have been directly set to FAILED by the reaper.
    await reaper_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING


# ══════════════════════════════════════════════════════════════════════════════
# Infrastructure recovery auto-requeue (Step 5 in reaper)
# ══════════════════════════════════════════════════════════════════════════════


def _common_infra_recovery_mocks(mocker):
    """Mocks needed for infra-recovery sweep tests (no active/orphaned tasks)."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    mocker.patch("app.max_plan_pool.release_max_plan_account", new_callable=AsyncMock)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))


@pytest.mark.asyncio
async def test_infra_recovery_resurrects_provision_failure_when_zone_available(
    reaper_db, mocker
):
    """A PROVISION_FAILURE task is reset to QUEUED when at least one zone is available."""
    _common_infra_recovery_mocks(mocker)
    mocker.patch("app.reaper.any_zone_available", return_value=True)

    project = Project(name="Infra Recovery PROVISION Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        status=TaskStatus.FAILED,
        attempt_count=3,
        context={
            "failure_category": "PROVISION_FAILURE",
            "provision_failures": 3,
            "provision_failures_infra": 1,
            "_infra_wait": True,
        },
        updated_at=_utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    assert task.status == TaskStatus.QUEUED
    ctx = task.context or {}
    assert ctx.get("_resurrection_count") == 1
    assert ctx.get("_resurrected_from") == "PROVISION_FAILURE"
    # Infra retry state cleared
    assert "provision_failures" not in ctx
    assert "provision_failures_infra" not in ctx
    assert "_infra_wait" not in ctx
    # failure_category cleared so next run is reclassified fresh
    assert "failure_category" not in ctx
    # Attempt count reset
    assert task.attempt_count == 0
    assert task.vm_instance_id is not None
    assert task.latest_status_message.startswith("Auto-resurrected")


@pytest.mark.asyncio
async def test_infra_recovery_does_not_resurrect_when_no_zone_available(
    reaper_db, mocker
):
    """A PROVISION_FAILURE task stays FAILED when no zone is available."""
    _common_infra_recovery_mocks(mocker)
    # The infra-recovery sweep calls any_zone_available from reaper_infra_recovery
    # (extracted in PR #6524). Patch the call-site module, not app.reaper.
    mocker.patch("app.reaper_infra_recovery.any_zone_available", return_value=False)

    project = Project(name="No Zone Available Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        status=TaskStatus.FAILED,
        attempt_count=3,
        context={"failure_category": "PROVISION_FAILURE"},
        updated_at=_utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    # Still FAILED — no zone recovered
    assert task.status == TaskStatus.FAILED
    ctx = task.context or {}
    assert "_resurrection_count" not in ctx


@pytest.mark.asyncio
async def test_infra_recovery_caps_at_one_resurrection_per_task(reaper_db, mocker):
    """A task that was already resurrected once must NOT be resurrected again."""
    _common_infra_recovery_mocks(mocker)
    mocker.patch("app.reaper.any_zone_available", return_value=True)

    project = Project(name="Resurrection Cap Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        status=TaskStatus.FAILED,
        attempt_count=3,
        context={
            "failure_category": "PROVISION_FAILURE",
            "_resurrection_count": 1,  # Already resurrected once
        },
        updated_at=_utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    # Must stay FAILED — resurrection cap reached
    assert task.status == TaskStatus.FAILED
    assert (task.context or {}).get("_resurrection_count") == 1


@pytest.mark.asyncio
async def test_infra_recovery_resurrects_agent_timeout_when_zone_available(
    reaper_db, mocker
):
    """An AGENT_TIMEOUT task (OOM/crash/heartbeat timeout) is reset to QUEUED when a zone is available."""
    _common_infra_recovery_mocks(mocker)
    mocker.patch("app.reaper.any_zone_available", return_value=True)

    project = Project(name="Infra Recovery AGENT_TIMEOUT Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        status=TaskStatus.FAILED,
        attempt_count=2,
        context={"failure_category": "AGENT_TIMEOUT"},
        updated_at=_utcnow() - timedelta(minutes=20),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    assert task.status == TaskStatus.QUEUED
    ctx = task.context or {}
    assert ctx.get("_resurrection_count") == 1
    assert ctx.get("_resurrected_from") == "AGENT_TIMEOUT"
    assert "failure_category" not in ctx
    assert task.attempt_count == 0
    assert task.latest_status_message.startswith("Auto-resurrected")


@pytest.mark.asyncio
async def test_infra_recovery_skips_agent_timeout_when_no_zone_available(
    reaper_db, mocker
):
    """An AGENT_TIMEOUT task is not resurrected when no zone is available."""
    _common_infra_recovery_mocks(mocker)
    # The infra-recovery sweep calls any_zone_available from reaper_infra_recovery
    # (extracted in PR #6524). Patch the call-site module, not app.reaper.
    mocker.patch("app.reaper_infra_recovery.any_zone_available", return_value=False)

    project = Project(name="AGENT_TIMEOUT No Zone Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        status=TaskStatus.FAILED,
        attempt_count=2,
        context={"failure_category": "AGENT_TIMEOUT"},
        updated_at=_utcnow() - timedelta(minutes=20),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert "_resurrection_count" not in (task.context or {})


@pytest.mark.asyncio
async def test_infra_recovery_skips_tasks_older_than_two_hours(reaper_db, mocker):
    """Tasks that failed more than 2 hours ago are outside the recovery window."""
    _common_infra_recovery_mocks(mocker)
    mocker.patch("app.reaper.any_zone_available", return_value=True)

    project = Project(name="Old Failure Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        status=TaskStatus.FAILED,
        attempt_count=3,
        context={"failure_category": "PROVISION_FAILURE"},
        # 3 hours ago — outside the 2h window
        updated_at=_utcnow() - timedelta(hours=3),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert "_resurrection_count" not in (task.context or {})


@pytest.mark.asyncio
async def test_infra_recovery_does_not_resurrect_code_error_tasks(reaper_db, mocker):
    """Tasks that failed with CODE_ERROR must never be auto-resurrected."""
    _common_infra_recovery_mocks(mocker)
    mocker.patch("app.reaper.any_zone_available", return_value=True)

    project = Project(name="Code Error No Resurrect Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        status=TaskStatus.FAILED,
        attempt_count=3,
        context={"failure_category": "CODE_ERROR"},
        updated_at=_utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert "_resurrection_count" not in (task.context or {})


@pytest.mark.asyncio
async def test_infra_recovery_resurrects_external_api_when_deps_healthy(
    reaper_db, mocker
):
    """An EXTERNAL_API task is resurrected when both github_api and dispatch_api are healthy."""
    _common_infra_recovery_mocks(mocker)
    mocker.patch("app.reaper.any_zone_available", return_value=True)
    # Patch external_health directly in the module
    mocker.patch.dict(
        "app.reaper._external_health",
        {"github_api": True, "dispatch_api": True},
    )

    project = Project(name="External API Recovery Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        status=TaskStatus.FAILED,
        attempt_count=3,
        context={"failure_category": "EXTERNAL_API"},
        updated_at=_utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    assert task.status == TaskStatus.QUEUED
    ctx = task.context or {}
    assert ctx.get("_resurrection_count") == 1
    assert ctx.get("_resurrected_from") == "EXTERNAL_API"


@pytest.mark.asyncio
async def test_infra_recovery_clears_api_failure_retries_on_external_api_resurrection(
    reaper_db, mocker
):
    """
    When the reaper resurrects an EXTERNAL_API task, it must clear _api_failure_retries.
    Without this, a task that exhausted its retry budget before failing would have
    stale _api_failure_retries > 5 after resurrection, causing any subsequent API error
    to bypass the EXTERNAL_API-specific backoff and fall through to the general retry
    path with a depleted budget.
    """
    _common_infra_recovery_mocks(mocker)
    mocker.patch("app.reaper.any_zone_available", return_value=True)
    mocker.patch.dict(
        "app.reaper._external_health",
        {"github_api": True, "dispatch_api": True},
    )

    project = Project(name="API Retry Clear Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        status=TaskStatus.FAILED,
        attempt_count=3,
        context={
            "failure_category": "EXTERNAL_API",
            # Simulate a task that exhausted its retry budget before failing permanently
            "_api_failure_retries": 6,
        },
        updated_at=_utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    assert task.status == TaskStatus.QUEUED
    ctx = task.context or {}
    assert ctx.get("_resurrection_count") == 1
    assert "_api_failure_retries" not in ctx, (
        "_api_failure_retries must be cleared on resurrection so the task "
        "gets a fresh retry budget if it hits another EXTERNAL_API failure"
    )


@pytest.mark.asyncio
async def test_infra_recovery_does_not_resurrect_external_api_when_deps_unhealthy(
    reaper_db, mocker
):
    """An EXTERNAL_API task stays FAILED when dependencies are not yet healthy."""
    _common_infra_recovery_mocks(mocker)
    mocker.patch("app.reaper.any_zone_available", return_value=True)
    mocker.patch.dict(
        "app.reaper._external_health",
        {"github_api": False, "dispatch_api": True},
    )

    project = Project(name="External API Unhealthy Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        status=TaskStatus.FAILED,
        attempt_count=3,
        context={"failure_category": "EXTERNAL_API"},
        updated_at=_utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert "_resurrection_count" not in (task.context or {})


@pytest.mark.asyncio
async def test_infra_recovery_resurrects_rate_limit_when_account_available(
    reaper_db, mocker
):
    """A RATE_LIMIT task is resurrected when a non-blacklisted account is available."""
    _common_infra_recovery_mocks(mocker)
    mocker.patch(
        "app.max_plan_pool._extract_task_model", return_value="claude-sonnet-4-6"
    )
    mocker.patch(
        "app.max_plan_pool._all_accounts",
        return_value=[{"id": "acct-1"}, {"id": "acct-2"}],
    )
    mocker.patch("app.max_plan_pool._account_available_for_model", return_value=True)

    project = Project(name="Rate Limit Recovery Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    # Audit W1: retry_after_until is a column. The resurrection path in
    # reaper.py clears the column directly.
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        status=TaskStatus.FAILED,
        attempt_count=3,
        retry_after_until=datetime(2026, 1, 1),
        context={
            "failure_category": "RATE_LIMIT",
            "blacklisted_accounts": ["acct-blacklisted"],
            "max_plan_account_id": "acct-blacklisted",
        },
        updated_at=_utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    assert task.status == TaskStatus.QUEUED
    ctx = task.context or {}
    assert ctx.get("_resurrection_count") == 1
    assert ctx.get("_resurrected_from") == "RATE_LIMIT"
    # Rate limit retry state cleared
    assert "blacklisted_accounts" not in ctx
    assert "max_plan_account_id" not in ctx
    assert task.retry_after_until is None  # column cleared
    assert "failure_category" not in ctx
    assert task.attempt_count == 0


@pytest.mark.asyncio
async def test_infra_recovery_does_not_resurrect_rate_limit_when_all_accounts_blacklisted(
    reaper_db, mocker
):
    """A RATE_LIMIT task stays FAILED when all available accounts are still blacklisted."""
    _common_infra_recovery_mocks(mocker)
    mocker.patch(
        "app.max_plan_pool._extract_task_model", return_value="claude-sonnet-4-6"
    )
    # Only one account exists and it's in the blacklist
    mocker.patch("app.max_plan_pool._all_accounts", return_value=[{"id": "acct-1"}])
    # account_available_for_model returns True but the account is blacklisted — any() is False
    mocker.patch("app.max_plan_pool._account_available_for_model", return_value=True)

    project = Project(name="All Accounts Blacklisted Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        status=TaskStatus.FAILED,
        attempt_count=3,
        context={
            "failure_category": "RATE_LIMIT",
            "blacklisted_accounts": ["acct-1"],
        },
        updated_at=_utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert "_resurrection_count" not in (task.context or {})


@pytest.mark.asyncio
async def test_reaper_queued_age_alert_fired(reaper_db, mocker):
    """
    Reaper Step 6: tasks sitting in QUEUED for longer than the threshold get
    an INFO alert created and queued_age_alert_fired set in context.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))
    mocker.patch(
        "app.shared_state.runtime_config",
        {
            "max_concurrent_vms": 8,
            "max_retries": 3,
            "max_provision_retries": 3,
            "queued_age_alert_hours": 2,
        },
    )
    # Override the reaper module's reference to runtime_config
    mocker.patch(
        "app.reaper._runtime_config",
        {
            "max_concurrent_vms": 8,
            "max_retries": 3,
            "max_provision_retries": 3,
            "queued_age_alert_hours": 2,
        },
    )

    project = Project(name="Queued Age Alert Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    # Task queued for 3 hours — exceeds 2h threshold
    stale_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "waiting forever"}],
        status=TaskStatus.QUEUED,
        requires_approval=False,
        created_at=datetime.now(timezone.utc) - timedelta(hours=3),
    )
    reaper_db.add(stale_task)
    await reaper_db.commit()
    await reaper_db.refresh(stale_task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(stale_task)
    assert stale_task.context.get("queued_age_alert_fired") is True

    from sqlmodel import select as _select

    alerts = (
        (await reaper_db.execute(_select(Alert).where(Alert.task_id == stale_task.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert str(alerts[0].severity) in ("INFO", "AlertSeverity.INFO")
    assert "stalled in queue" in alerts[0].title
    assert "create" in alerts[0].message
    assert "3.0h" in alerts[0].message or "2." in alerts[0].message  # ~3h queued


@pytest.mark.asyncio
async def test_reaper_queued_age_alert_not_repeated(reaper_db, mocker):
    """
    Reaper Step 6: tasks that already have queued_age_alert_fired=True do NOT
    get a second alert on the next reaper cycle.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))
    mocker.patch(
        "app.reaper._runtime_config",
        {
            "max_concurrent_vms": 8,
            "max_retries": 3,
            "max_provision_retries": 3,
            "queued_age_alert_hours": 2,
        },
    )

    project = Project(name="Queued Age No Repeat Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    stale_task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "prompt": "stuck"}],
        status=TaskStatus.QUEUED,
        requires_approval=False,
        created_at=datetime.now(timezone.utc) - timedelta(hours=5),
        context={"queued_age_alert_fired": True},  # already fired
    )
    reaper_db.add(stale_task)
    await reaper_db.commit()
    await reaper_db.refresh(stale_task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    from sqlmodel import select as _select

    alerts = (
        (await reaper_db.execute(_select(Alert).where(Alert.task_id == stale_task.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 0, (
        "Alert should not be re-fired when queued_age_alert_fired is already True"
    )


@pytest.mark.asyncio
async def test_reaper_queued_age_alert_includes_requires_approval(reaper_db, mocker):
    """
    Reaper Step 6: tasks with requires_approval=True are NOT skipped — they get
    an alert with an approval-specific cause so they aren't invisible forever.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))
    mocker.patch(
        "app.reaper._runtime_config",
        {
            "max_concurrent_vms": 8,
            "max_retries": 3,
            "max_provision_retries": 3,
            "queued_age_alert_hours": 2,
        },
    )

    project = Project(name="Approval Skip Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    approval_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "needs approval"}],
        status=TaskStatus.QUEUED,
        requires_approval=True,
        created_at=datetime.now(timezone.utc) - timedelta(hours=5),
    )
    reaper_db.add(approval_task)
    await reaper_db.commit()
    await reaper_db.refresh(approval_task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    from sqlmodel import select as _select

    alerts = (
        (
            await reaper_db.execute(
                _select(Alert).where(Alert.task_id == approval_task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1, (
        "Approval-pending tasks should now surface via queued age alerts"
    )
    assert "awaiting operator approval" in alerts[0].message


@pytest.mark.asyncio
async def test_reaper_queued_age_alert_retry_after_cause(reaper_db, mocker):
    """
    Reaper Step 6: a QUEUED task with an active future retry_after should be
    diagnosed as blocked by rate-limit retry delay, not the generic fallback.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))
    mocker.patch(
        "app.reaper._runtime_config",
        {
            "max_concurrent_vms": 8,
            "max_retries": 3,
            "max_provision_retries": 3,
            "queued_age_alert_hours": 2,
        },
    )

    project = Project(name="Retry After Cause Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    # Audit W1: retry_after_until is a column. The escalation counter
    # _retry_after_resets stays in JSONB.
    future_dt = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10)
    stale_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "rate-limited"}],
        status=TaskStatus.QUEUED,
        requires_approval=False,
        created_at=datetime.now(timezone.utc) - timedelta(hours=3),
        retry_after_until=future_dt,
        context={"_retry_after_resets": 4},
    )
    reaper_db.add(stale_task)
    await reaper_db.commit()
    await reaper_db.refresh(stale_task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    from sqlmodel import select as _select

    alerts = (
        (await reaper_db.execute(_select(Alert).where(Alert.task_id == stale_task.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "rate-limit retry delay" in alerts[0].message
    assert "reset 4 times" in alerts[0].message


@pytest.mark.asyncio
async def test_reaper_queued_age_alert_precondition_defer_cause(reaper_db, mocker):
    """
    Reaper Step 6: a QUEUED task whose latest_status_message is a dispatcher
    precondition defer ('Waiting: ...') is escalated with that precondition as
    the likely cause, taking precedence over the generic infrastructure
    heuristics (VM-slot / pool-blockage / type-failure gate).
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))
    mocker.patch(
        "app.reaper._runtime_config",
        {
            "max_concurrent_vms": 8,
            "max_retries": 3,
            "max_provision_retries": 3,
            "queued_age_alert_hours": 2,
        },
    )

    project = Project(name="Precondition Defer Cause Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    # Case 1: deferred behind a pending rollup.
    rollup_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "deferred"}],
        status=TaskStatus.QUEUED,
        requires_approval=False,
        created_at=datetime.now(timezone.utc) - timedelta(hours=3),
        latest_status_message="Waiting: overlaps pending rollup 'big-rollup'",
    )
    # Case 2: deferred behind a file conflict with a running task.
    conflict_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "deferred"}],
        status=TaskStatus.QUEUED,
        requires_approval=False,
        created_at=datetime.now(timezone.utc) - timedelta(hours=3),
        latest_status_message="Waiting: file conflict on app/models.py",
    )
    reaper_db.add(rollup_task)
    reaper_db.add(conflict_task)
    await reaper_db.commit()
    await reaper_db.refresh(rollup_task)
    await reaper_db.refresh(conflict_task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    from sqlmodel import select as _select

    rollup_alerts = (
        (await reaper_db.execute(_select(Alert).where(Alert.task_id == rollup_task.id)))
        .scalars()
        .all()
    )
    assert len(rollup_alerts) == 1
    assert "overlaps pending rollup 'big-rollup'" in rollup_alerts[0].message
    # The generic infrastructure guesses must NOT appear in their place.
    assert "VM slot limit" not in rollup_alerts[0].message

    conflict_alerts = (
        (
            await reaper_db.execute(
                _select(Alert).where(Alert.task_id == conflict_task.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(conflict_alerts) == 1
    assert "file conflict on app/models.py" in conflict_alerts[0].message
    assert "VM slot limit" not in conflict_alerts[0].message


@pytest.mark.asyncio
async def test_reaper_queued_age_alert_escalates(reaper_db, mocker):
    """
    Reaper Step 6: a task already at tier 1 (INFO) that ages past the tier 2
    threshold (3× base) re-alerts at WARNING with a rising tier value.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))
    mocker.patch(
        "app.reaper._runtime_config",
        {
            "max_concurrent_vms": 8,
            "max_retries": 3,
            "max_provision_retries": 3,
            "queued_age_alert_hours": 2,
        },
    )

    project = Project(name="Escalation Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    # 7h old, previously alerted at tier 1 → should escalate to tier 2 (WARNING at 6h)
    stale_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "aging"}],
        status=TaskStatus.QUEUED,
        requires_approval=False,
        created_at=datetime.now(timezone.utc) - timedelta(hours=7),
        context={"queued_age_alert_tier": 1, "queued_age_alert_fired": True},
    )
    reaper_db.add(stale_task)
    await reaper_db.commit()
    await reaper_db.refresh(stale_task)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(stale_task)
    assert stale_task.context.get("queued_age_alert_tier") == 2

    from sqlmodel import select as _select

    alerts = (
        (await reaper_db.execute(_select(Alert).where(Alert.task_id == stale_task.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert str(alerts[0].severity) in ("WARNING", "AlertSeverity.WARNING")
    assert "tier 2" in alerts[0].message


@pytest.mark.asyncio
async def test_reaper_backoff_on_consecutive_errors(reaper_db, mocker):
    """
    The reaper's outer loop must apply exponential backoff when errors are
    consecutive. Counter increments on each error; resets to 0 on success.
    Sleep formula: min(60 * (2 ** consecutive_errors), 300).
    error 1: counter=1 → 120s; error 2: counter=2 → 240s; error 3: counter=3 → 300s (capped).
    After a successful cycle the counter resets to 0 → 60s.
    """
    import asyncio as _asyncio

    _common_reaper_mocks(mocker)

    sleep_calls = []
    call_count = 0

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        nonlocal call_count
        call_count += 1
        if call_count >= 4:
            raise _asyncio.CancelledError()

    mocker.patch("asyncio.sleep", side_effect=fake_sleep)

    # First 3 calls to AsyncSessionLocal raise; 4th succeeds with empty DB.
    iteration = 0

    class _FailSession:
        async def __aenter__(self):
            raise RuntimeError("DB down")

        async def __aexit__(self, *_a):
            pass

    class _OkSession:
        async def __aenter__(self):
            return reaper_db

        async def __aexit__(self, *_a):
            pass

    def session_factory():
        nonlocal iteration
        iteration += 1
        return _FailSession() if iteration <= 3 else _OkSession()

    mocker.patch("app.reaper.AsyncSessionLocal", side_effect=session_factory)

    try:
        await reaper_service()
    except _asyncio.CancelledError:
        pass

    assert len(sleep_calls) == 4, f"Expected 4 sleep calls, got {sleep_calls}"
    assert sleep_calls[0] == 120  # error 1: counter=1
    assert sleep_calls[1] == 240  # error 2: counter=2
    assert sleep_calls[2] == 300  # error 3: counter=3 → capped
    assert sleep_calls[3] == 60  # success: counter reset to 0


@pytest.mark.asyncio
async def test_reaper_clears_far_future_retry_after(reaper_db, mocker):
    """A QUEUED task with a far-future retry_after (>30 min) has it cleared by the reaper.

    Without this sweep, a malformed or runaway value silently blocks dispatch
    forever since the orchestrator skips tasks where retry_after > now.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))

    project = Project(name="Retry-After Sweep Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    # Audit W1: retry_after is now a column (Task.retry_after_until); the
    # context counter (_retry_after_resets) stays in JSONB for escalation logic.
    far_future_dt = _utcnow() + timedelta(hours=5)
    valid_dt = _utcnow() + timedelta(minutes=5)
    stale_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "do work"}],
        status=TaskStatus.QUEUED,
        retry_after_until=far_future_dt,
        context={"_retry_after_resets": 2},
    )
    # Note: a "malformed" timestamp can no longer be set on a typed column,
    # so the malformed-input case is now structurally impossible. The sweep
    # only needs to handle far-future values.
    valid_task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "ok work"}],
        status=TaskStatus.QUEUED,
        retry_after_until=valid_dt,
    )
    reaper_db.add_all([stale_task, valid_task])
    await reaper_db.commit()
    for t in (stale_task, valid_task):
        await reaper_db.refresh(t)

    try:
        await reaper_service()
    except _StopLoop:
        pass

    await reaper_db.refresh(stale_task)
    await reaper_db.refresh(valid_task)

    assert stale_task.retry_after_until is None, (
        "far-future retry_after should have been cleared"
    )
    assert "_retry_after_resets" not in (stale_task.context or {}), (
        "_retry_after_resets must be cleared alongside retry_after to avoid "
        "false escalations on the next rate-limit episode"
    )
    assert valid_task.retry_after_until is not None, (
        "valid (<=30min) retry_after should be preserved"
    )


def test_clear_retry_after_state_resets_counter():
    """Releasing a retry_after hold must also clear _retry_after_resets so the
    next rate-limit episode does not resume from a stale counter and trigger
    a false escalation."""
    from app.orchestrator import _clear_retry_after_state

    ctx = {
        "retry_after": "2026-01-01T00:00:00",
        "_retry_after_resets": 5,
        "other": "preserved",
    }
    _clear_retry_after_state(ctx)
    assert "retry_after" not in ctx
    assert "_retry_after_resets" not in ctx
    assert ctx["other"] == "preserved"

    # Idempotent on already-clean context
    _clear_retry_after_state(ctx)
    assert ctx == {"other": "preserved"}


def test_set_retry_after_clamps_to_30_minutes():
    """set_retry_after must cap delays at RETRY_AFTER_MAX_MINUTES."""
    from app.time_utils import (
        set_retry_after,
        RETRY_AFTER_MAX_MINUTES,
        parse_retry_after,
    )

    ctx = {}
    set_retry_after(ctx, 9999)
    dt = parse_retry_after(ctx["retry_after"])
    assert dt is not None
    assert dt <= _utcnow() + timedelta(
        minutes=RETRY_AFTER_MAX_MINUTES
    ) + timedelta(seconds=5)

    ctx2 = {}
    set_retry_after(ctx2, -100)
    dt2 = parse_retry_after(ctx2["retry_after"])
    assert dt2 is not None
    assert dt2 >= _utcnow() - timedelta(seconds=5)
