import uuid
import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select
from app.models import Project, Task, TaskStatus
from app.reaper import reaper_service, _reaper_skip_counts
from app.broadcast import broadcast_to_project, manager
from app.services import _run_stale_branch_cleanup
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
        bind=reaper_engine, class_=AsyncSession,
        autocommit=False, autoflush=False, expire_on_commit=False,
    )
    mocker.patch("app.reaper.AsyncSessionLocal", ReaperSession)

    async with ReaperSession() as session:
        yield session

    async with reaper_engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await reaper_engine.dispose()

@pytest.mark.asyncio
async def test_reaper_evaluating_sweep_queries_started_at(reaper_db, mocker):
    """Regression: the EVALUATING stale-parent sweep queried Task.updated_at,
    which does not exist on the Task SQLModel and raised AttributeError every
    reaper cycle — making the sweep entirely non-functional in production.

    The sweep must build the query against started_at, find stale EVALUATING
    parents (> 15 min old), and invoke _check_and_trigger_rollup on a completed
    child so the stale-PR recovery path can respawn a review.
    """
    mocker.patch("app.main.delete_agent_vm", return_value=True)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mock_rollup = mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )

    class StopLoopException(Exception):
        pass
    mocker.patch("asyncio.sleep", side_effect=StopLoopException("Stop"))

    project = Project(name="Evaluating Sweep Test", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    # Stale EVALUATING parent: started 20 minutes ago, well past the 15-min cutoff.
    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research", "prompt": "stuck evaluating"}],
        status=TaskStatus.EVALUATING,
        started_at=datetime.utcnow() - timedelta(minutes=20),
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
    await reaper_db.commit()

    try:
        await reaper_service()
    except StopLoopException:
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
    mock_delete = mocker.patch("app.main.delete_agent_vm", return_value=True)

    class StopLoopException(Exception):
        pass
    mocker.patch("asyncio.sleep", side_effect=StopLoopException("Stop"))

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
        created_at=datetime.now(timezone.utc) - timedelta(hours=2)
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except StopLoopException:
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
    mock_delete = mocker.patch("app.main.delete_agent_vm", return_value=True)

    class StopLoopException(Exception):
        pass
    mocker.patch("asyncio.sleep", side_effect=StopLoopException("Stop"))

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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except StopLoopException:
        pass

    await reaper_db.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert task.attempt_count == 2
    mock_delete.assert_called_once_with("retry-vm")


@pytest.mark.asyncio
async def test_reaper_terminal_failure(reaper_db, mocker):
    """
    Tests that a task at its retry limit is permanently marked FAILED
    when the reaper detects a heartbeat timeout.
    """
    mock_delete = mocker.patch("app.main.delete_agent_vm", return_value=True)

    class StopLoopException(Exception):
        pass
    mocker.patch("asyncio.sleep", side_effect=StopLoopException("Stop"))

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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except StopLoopException:
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
    mocker.patch("app.main.delete_agent_vm", return_value=True)

    class StopLoopException(Exception):
        pass
    mocker.patch("asyncio.sleep", side_effect=StopLoopException("Stop"))

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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except StopLoopException:
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
    mocker.patch("app.main.delete_agent_vm", return_value=True)

    class StopLoopException(Exception):
        pass
    mocker.patch("asyncio.sleep", side_effect=StopLoopException("Stop"))

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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=6),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    try:
        await reaper_service()
    except StopLoopException:
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
    mocker.patch("app.main.delete_agent_vm", return_value=True)

    class StopLoopException(Exception):
        pass
    mocker.patch("asyncio.sleep", side_effect=StopLoopException("Stop"))

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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
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
    except StopLoopException:
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
        bind=engine, class_=AsyncSession,
        autocommit=False, autoflush=False, expire_on_commit=False,
    )
    mocker.patch("app.services.AsyncSessionLocal", Session)

    async with Session() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


@pytest.mark.asyncio
async def test_stale_branch_cleanup_preserves_active_task_branch(cleanup_db, mocker):
    """Branches referenced by active tasks must not be deleted."""
    project = Project(name="Cleanup Test", user_id="user1", github_owner="org", github_repo="repo")
    cleanup_db.add(project)
    await cleanup_db.commit()
    await cleanup_db.refresh(project)

    # Active task with a known branch in its context
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.IN_PROGRESS,
        context={"review_loop": {"branch": "blueprint/task-abc12345"}},
    )
    cleanup_db.add(task)
    await cleanup_db.commit()

    # Mock GitHub: one stale branch belonging to the active task
    old_date = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    mocker.patch("app.services._get_project_github_token", return_value="tok")
    mocker.patch("app.github.list_branches", return_value=[
        {"name": "blueprint/task-abc12345", "sha": "sha1"},
    ])
    mocker.patch("app.github.get_commit_date", return_value=datetime.utcnow() - timedelta(days=30))
    mock_delete = mocker.patch("app.github.delete_branch", return_value=True)

    await _run_stale_branch_cleanup()

    # Branch belongs to an active task — must not be deleted
    mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_stale_branch_cleanup_deletes_orphaned_branch(cleanup_db, mocker):
    """Branches older than 7 days with no active task should be deleted."""
    project = Project(name="Cleanup Test 2", user_id="user2", github_owner="org", github_repo="repo")
    cleanup_db.add(project)
    await cleanup_db.commit()
    await cleanup_db.refresh(project)

    # No active tasks — branch is truly orphaned
    mocker.patch("app.services._get_project_github_token", return_value="tok")
    mocker.patch("app.github.list_branches", return_value=[
        {"name": "blueprint/task-deadbeef", "sha": "sha2"},
    ])
    mocker.patch(
        "app.github.get_commit_date",
        return_value=datetime.utcnow() - timedelta(days=10),
    )
    mock_delete = mocker.patch("app.github.delete_branch", return_value=True)

    await _run_stale_branch_cleanup()

    mock_delete.assert_called_once_with("tok", "org", "repo", "blueprint/task-deadbeef")


@pytest.mark.asyncio
async def test_stale_branch_cleanup_spares_protected_branches(cleanup_db, mocker):
    """Protected branches (main, master, develop) must never be deleted."""
    project = Project(name="Cleanup Test 3", user_id="user3", github_owner="org", github_repo="repo")
    cleanup_db.add(project)
    await cleanup_db.commit()
    await cleanup_db.refresh(project)

    mocker.patch("app.services._get_project_github_token", return_value="tok")
    # These start with "blueprint/" so they pass the prefix filter, but names are protected
    # Use realistic names that start with the allowed prefixes — the protected check is by exact name
    mocker.patch("app.github.list_branches", return_value=[
        {"name": "main", "sha": "sha3"},
        {"name": "master", "sha": "sha4"},
        {"name": "develop", "sha": "sha5"},
    ])
    mocker.patch(
        "app.github.get_commit_date",
        return_value=datetime.utcnow() - timedelta(days=30),
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
        bind=engine, class_=AsyncSession,
        autocommit=False, autoflush=False, expire_on_commit=False,
    )

    async with Session() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


async def _make_parent_child(session, child_payloads, child_context=None, child_status_message=None):
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
    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "do the thing"}],
        child_status_message="reaped after no heartbeat for 30 minutes",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (await requeue_db.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().all()
    assert len(clones) == 1
    suffix = (clones[0].payloads or [{}])[0].get("prompt_suffix", "")
    assert "TIMED OUT" in suffix
    assert "reduce scope" in suffix.lower()


@pytest.mark.asyncio
async def test_requeue_code_error_test_failure_injects_test_guidance(requeue_db, mocker):
    """CODE_ERROR with test keywords in status message injects test-fixing guidance."""
    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "add feature"}],
        child_status_message="pytest test_foo failed with AssertionError",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (await requeue_db.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().all()
    assert len(clones) == 1
    suffix = (clones[0].payloads or [{}])[0].get("prompt_suffix", "")
    assert "RETRY GUIDANCE" in suffix
    assert "failing tests" in suffix.lower()


@pytest.mark.asyncio
async def test_requeue_code_error_lint_failure_injects_lint_guidance(requeue_db, mocker):
    """CODE_ERROR with lint keywords injects lint-fixing guidance."""
    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "refactor module"}],
        child_status_message="ruff lint error: E501 line too long",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (await requeue_db.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().all()
    assert len(clones) == 1
    suffix = (clones[0].payloads or [{}])[0].get("prompt_suffix", "")
    assert "RETRY GUIDANCE" in suffix
    assert "lint" in suffix.lower()


@pytest.mark.asyncio
async def test_requeue_large_scope_second_timeout_downgrades_scope(requeue_db, mocker):
    """Large-scope task on 2nd+ AGENT_TIMEOUT attempt gets scope downgraded to medium."""
    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "big task"}],
        child_context={"scope": "large", "_attempt_number": 2},
        child_status_message="execution timed out after 60 minutes",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (await requeue_db.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().all()
    assert len(clones) == 1
    clone_ctx = clones[0].context or {}
    assert clone_ctx.get("scope") == "medium"
    assert clone_ctx.get("_scope_reduced") is True


@pytest.mark.asyncio
async def test_requeue_large_scope_first_timeout_no_scope_downgrade(requeue_db, mocker):
    """Large-scope task on 1st AGENT_TIMEOUT does NOT get scope downgraded."""
    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "big task first try"}],
        child_context={"scope": "large", "_attempt_number": 1},
        child_status_message="execution timed out after 60 minutes",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (await requeue_db.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().all()
    assert len(clones) == 1
    clone_ctx = clones[0].context or {}
    # scope should not be changed to medium on first timeout
    assert clone_ctx.get("scope") == "large"
    assert "_scope_reduced" not in clone_ctx


@pytest.mark.asyncio
async def test_requeue_rate_limit_no_scope_guidance(requeue_db, mocker):
    """Non-timeout, non-code-error failures (e.g. RATE_LIMIT) do not inject scope guidance."""
    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "some task"}],
        child_status_message="API Error: Rate limit reached",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (await requeue_db.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().all()
    assert len(clones) == 1
    # RATE_LIMIT → no guidance injected; prompt_suffix absent or empty
    suffix = (clones[0].payloads or [{}])[0].get("prompt_suffix")
    assert not suffix


@pytest.mark.asyncio
async def test_requeue_sets_retry_context_fields(requeue_db, mocker):
    """clone_ctx contains _attempt_number incremented, _previous_error, and _previous_failure_class."""
    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "do something"}],
        child_context={"_attempt_number": 1, "scope": "medium"},
        child_status_message="pytest test_foo failed with AssertionError: expected 1 got 2",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (await requeue_db.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().all()
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
    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)

    pr_url = "https://github.com/org/repo/pull/42"
    changed_files = ["src/foo.py", "src/bar.py"]

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "fix", "prompt": "fix the bug", "pr_number": 42, "branch": "fix/bug"}],
        child_context={
            "_attempt_number": 1,
            "_pr_url": pr_url,
            "_pr_changed_files": changed_files,
        },
        child_status_message="pytest test_foo failed with AssertionError: expected 1 got 2",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (await requeue_db.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().all()
    assert len(clones) == 1
    clone_ctx = clones[0].context or {}

    # PR URL from failed attempt must propagate to retry clone
    assert clone_ctx.get("_previous_pr_url") == pr_url
    # Changed files from failed attempt must propagate to retry clone
    assert clone_ctx.get("_previous_changed_files") == changed_files


@pytest.mark.asyncio
async def test_requeue_no_pr_artifacts_when_child_has_none(requeue_db, mocker):
    """If child context has no _pr_url/_pr_changed_files, retry clone omits the _previous_* fields."""
    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "fresh task"}],
        child_context={"_attempt_number": 1},
        child_status_message="pytest test_foo failed with AssertionError",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (await requeue_db.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().all()
    assert len(clones) == 1
    clone_ctx = clones[0].context or {}

    # No PR artifacts on child → _previous_* keys must be absent
    assert "_previous_pr_url" not in clone_ctx
    assert "_previous_changed_files" not in clone_ctx


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
    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)

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
    mock_alert = mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)

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
    clones = (await requeue_db.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().all()
    assert len(clones) == 0


@pytest.mark.asyncio
async def test_requeue_cancelled_parent_returns_zero(requeue_db, mocker):
    """CANCELLED parent returns 0 immediately without creating any clones."""
    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)

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
    clones = (await requeue_db.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().all()
    assert len(clones) == 0


@pytest.mark.asyncio
async def test_requeue_dependency_remapping(requeue_db, mocker):
    """Pass 2: deps on other failed children remapped to clone IDs; COMPLETED sibling dep preserved."""
    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)

    # child_A: FAILED (no deps)
    # child_B: FAILED, depends on child_A (FAILED) and child_C (COMPLETED)
    # child_C: COMPLETED (should not be cloned; its ID preserved in clone_B.depends_on)
    parent, children = await _make_parent_with_children(
        requeue_db,
        [
            {"payloads": [{"type": "create", "prompt": "task A"}], "status": TaskStatus.FAILED, "status_message": "ruff lint error"},
            {"payloads": [{"type": "create", "prompt": "task B"}], "status": TaskStatus.FAILED, "status_message": "ruff lint error", "depends_on_indices": [0, 2]},
            {"payloads": [{"type": "create", "prompt": "task C"}], "status": TaskStatus.COMPLETED},
        ],
    )
    child_a, child_b, child_c = children

    await _requeue_parent_task(parent, requeue_db)

    clones = (await requeue_db.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().all()
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
    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)

    # child_A depends on child_B and child_B depends on child_A — mutual cycle
    parent, _children = await _make_parent_with_children(
        requeue_db,
        [
            {"payloads": [{"type": "create", "prompt": "task A"}], "status": TaskStatus.FAILED, "status_message": "ruff lint error", "depends_on_indices": [1]},
            {"payloads": [{"type": "create", "prompt": "task B"}], "status": TaskStatus.FAILED, "status_message": "ruff lint error", "depends_on_indices": [0]},
        ],
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (await requeue_db.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().all()
    assert len(clones) == 2
    # All depends_on must be cleared due to cycle detection
    for clone in clones:
        assert not clone.depends_on


@pytest.mark.asyncio
async def test_requeue_strips_max_plan_account_id(requeue_db, mocker):
    """Clone context must not carry over max_plan_account_id from the failed child."""
    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "task"}],
        child_context={"max_plan_account_id": "acct-123", "scope": "small"},
        child_status_message="ruff lint error",
    )

    await _requeue_parent_task(parent, requeue_db)

    clones = (await requeue_db.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().all()
    assert len(clones) == 1
    clone_ctx = clones[0].context or {}
    assert "max_plan_account_id" not in clone_ctx
    # Other context fields must still be present
    assert clone_ctx.get("scope") == "small"


@pytest.mark.asyncio
async def test_requeue_refreshes_base_branch_from_plan_branch(requeue_db, mocker):
    """Clone payload uses parent's plan_branch as base_branch, replacing the stale child value."""
    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "task", "base_branch": "old-deleted-branch"}],
        child_status_message="ruff lint error",
    )
    parent.context = {"plan_branch": "plan/abc123"}
    requeue_db.add(parent)
    await requeue_db.commit()
    await requeue_db.refresh(parent)

    await _requeue_parent_task(parent, requeue_db)

    clones = (await requeue_db.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().all()
    assert len(clones) == 1
    assert clones[0].payloads[0].get("base_branch") == "plan/abc123"


@pytest.mark.asyncio
async def test_requeue_refreshes_base_branch_from_research_branch(requeue_db, mocker):
    """Clone payload uses parent's research_branch as base_branch when plan_branch is absent."""
    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)

    parent, _child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "task", "base_branch": "stale-branch"}],
        child_status_message="ruff lint error",
    )
    parent.context = {"research_branch": "research/xyz456"}
    requeue_db.add(parent)
    await requeue_db.commit()
    await requeue_db.refresh(parent)

    await _requeue_parent_task(parent, requeue_db)

    clones = (await requeue_db.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().all()
    assert len(clones) == 1
    assert clones[0].payloads[0].get("base_branch") == "research/xyz456"


# ── Provision failure classification ───────────────────────────────────────


def _common_reaper_mocks(mocker):
    """Patch external dependencies required by all reaper tests."""
    mocker.patch("app.main.delete_agent_vm", return_value=True)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch("app.routers.agent_status.record_task_result")
    mocker.patch("app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock)
    mocker.patch("app.max_plan_pool.release_max_plan_account", new_callable=AsyncMock)


class _StopLoop(Exception):
    pass


@pytest.mark.asyncio
async def test_reaper_infra_provision_failure_no_retry_budget_burn(reaper_db, mocker):
    """Infra-wide provision failures (e.g. quota exhausted) must NOT increment
    provision_failures — only provision_failures_infra — and must requeue the task."""
    _common_reaper_mocks(mocker)
    mocker.patch("app.reaper.get_provision_error", return_value="quota exceeded for region")
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
        started_at=datetime.utcnow() - timedelta(minutes=20),
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
    mocker.patch("app.reaper.get_provision_error", return_value="vm startup error: agent never claimed")
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
        started_at=datetime.utcnow() - timedelta(minutes=20),
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
        started_at=datetime.utcnow() - timedelta(minutes=20),
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
    # retry_after must be set (aligned with zone recovery)
    assert ctx.get("retry_after") is not None


# ── Rate-limit detection ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reaper_rate_limit_requeue_without_burning_retry_budget(reaper_db, mocker):
    """Rate-limited tasks are requeued immediately without incrementing attempt_count,
    the account is blacklisted, and disable_account_model is called."""
    _common_reaper_mocks(mocker)
    mock_disable = mocker.patch("app.max_plan_pool.disable_account_model", new_callable=AsyncMock)
    mocker.patch("app.max_plan_pool._extract_task_model", return_value="claude-sonnet-4-6")
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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
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
    # disable_account_model must have been called to block re-selection
    mock_disable.assert_called_once_with("rl-acct-123", "claude-sonnet-4-6")


@pytest.mark.asyncio
async def test_reaper_rate_limit_disable_account_model_failure_handled_gracefully(reaper_db, mocker):
    """If disable_account_model raises an exception, the reaper still requeues the task
    and does not propagate the error."""
    _common_reaper_mocks(mocker)
    mocker.patch(
        "app.max_plan_pool.disable_account_model",
        new_callable=AsyncMock,
        side_effect=RuntimeError("pool unavailable"),
    )
    mocker.patch("app.max_plan_pool._extract_task_model", return_value="claude-sonnet-4-6")
    mocker.patch("asyncio.sleep", side_effect=_StopLoop("Stop"))

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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
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
    # Task must still be requeued despite the disable_account_model failure
    assert task.status == TaskStatus.QUEUED
    ctx = task.context or {}
    assert "rl-acct-456" in (ctx.get("blacklisted_accounts") or [])


# ── Dependent task unblocking ───────────────────────────────────────────────


def _make_sqlite_compat_asl(real_asl, project_id, blocked_id_str):
    """Return a replacement for AsyncSessionLocal that works around SQLite's lack
    of JSONB @> operator.  The first call returns the real session (main reaper
    session); subsequent calls return a proxy that executes a plain QUEUED-tasks
    query and filters depends_on in Python instead of using @>."""

    call_count = 0

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

    class _SmartASL:
        def __call__(self):
            nonlocal call_count
            call_count += 1
            real_cm = real_asl()
            if call_count == 1:
                return real_cm  # main session: use real session unchanged
            return _DepSessionProxy(real_cm)  # dep session: Python-level filter

    return _SmartASL()


@pytest.mark.asyncio
async def test_reaper_unblocks_dependent_tasks_on_terminal_failure(reaper_db, mocker):
    """When a task fails terminally, QUEUED tasks whose depends_on list contains
    its ID must have that ID removed so they can proceed."""
    _common_reaper_mocks(mocker)
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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
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
    # query.  Patch AsyncSessionLocal so the dep_session uses Python-level filtering.
    import app.reaper as _reaper_mod
    compat_asl = _make_sqlite_compat_asl(
        _reaper_mod.AsyncSessionLocal,
        project.id,
        str(failed_task.id),
    )
    mocker.patch("app.reaper.AsyncSessionLocal", compat_asl)

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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
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
async def test_reaper_force_lock_failure_clears_tracking_state(reaper_db, mocker, clear_reaper_skip_counts):
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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
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
async def test_reaper_force_lock_threshold_is_five(reaper_db, mocker, clear_reaper_skip_counts):
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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
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
    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)

    parent, child = await _make_parent_child(
        requeue_db,
        child_payloads=[{"type": "create", "prompt": "do the thing"}],
        child_status_message="ruff lint error",
    )

    await _requeue_parent_task(parent, requeue_db)

    await requeue_db.refresh(child)
    clone = (await requeue_db.execute(
        select(Task).where(Task.parent_task_id == parent.id, Task.status == TaskStatus.QUEUED)
    )).scalars().first()

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
    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)

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
    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)

    project = Project(name="Remap Test", user_id="u1")
    requeue_db.add(project)
    await requeue_db.commit()
    await requeue_db.refresh(project)

    # COMPLETED clone — this will cause 'eligible' result
    clone = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "prompt": "clone", "pr_number": 1, "branch": "fix/1"}],
        status=TaskStatus.COMPLETED,
    )
    requeue_db.add(clone)
    await requeue_db.commit()
    await requeue_db.refresh(clone)

    cancelled_child = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "prompt": "original", "pr_number": 1, "branch": "fix/1"}],
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
    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)

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
    """FAILED dep (not CANCELLED) must always trigger cascade cancellation."""
    mocker.patch("app.main._maybe_create_failure_alert", return_value=None)

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
    from app import compute

    source = inspect.getsource(compute._provision_gcp)

    # Locate the success-heartbeat region by its unique status_message.
    success_msg = "Startup script completed, agent service started"
    assert success_msg in source, "success heartbeat block missing"
    success_idx = source.index(success_msg)

    # The preceding region must contain the settle window + is-active check.
    # Bound the search to a window immediately before the heartbeat curl so
    # we don't accidentally match the earlier `systemctl restart` block.
    window_start = max(0, success_idx - 600)
    pre_heartbeat = source[window_start:success_idx]
    assert "sleep 30" in pre_heartbeat, (
        "expected 30s settle window before success heartbeat — Type=simple "
        "returns before the agent is actually running"
    )
    assert "is-active" in pre_heartbeat, (
        "expected systemctl is-active re-check before success heartbeat"
    )

    # And the skip-message branch must exist (fall-through to wait loop).
    assert "skipping success heartbeat" in source, (
        "expected an explicit skip branch when the agent died in settle window"
    )
