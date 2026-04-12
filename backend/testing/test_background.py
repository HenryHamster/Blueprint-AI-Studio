import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from app.models import Project, Task, TaskStatus
from app.reaper import reaper_service
from app.broadcast import broadcast_to_project, manager
from app.services import _run_stale_branch_cleanup


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
