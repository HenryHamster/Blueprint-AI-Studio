import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from app.models import Project, Task, TaskStatus
from app.main import reaper_service, broadcast_to_project, manager


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
