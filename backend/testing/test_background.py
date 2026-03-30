import pytest
import asyncio
from datetime import datetime, timedelta
from app.models import Project, Task, TaskStatus
from app.main import reaper_service, broadcast_to_project, project_subscribers

@pytest.mark.asyncio
async def test_reaper_service(db_session, mocker):
    """
    Tests the background worker that terminates orphaned VMs.
    We mock asyncio.sleep to break the infinite loop after one iteration.
    """
    # 1. Mock delete_agent_vm so we don't hit GCP
    mock_delete = mocker.patch("app.main.delete_agent_vm", return_value=None)
    
    # 2. Mock asyncio.sleep to raise an exception to break the infinite loop
    class StopLoopException(Exception): pass
    mocker.patch("asyncio.sleep", side_effect=StopLoopException("Stop"))

    # 3. Create an orphaned task (older than 1 hour)
    project = Project(name="Reaper Test", user_id="user1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id, 
        payloads=[], 
        vm_instance_id="orphan-vm", 
        status=TaskStatus.IN_PROGRESS,
        created_at=datetime.utcnow() - timedelta(hours=2)
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # 4. Run the reaper service (it will crash out after one loop)
    try:
        await reaper_service()
    except StopLoopException:
        pass

    # 5. Verify the task was marked FAILED and the VM was deleted
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    mock_delete.assert_called_once_with("orphan-vm")

@pytest.mark.asyncio
async def test_broadcast_to_project():
    """
    Tests the in-memory SSE event bus.
    """
    project_id = "test-proj-1"
    queue = asyncio.Queue()
    
    # Subscribe a fake client
    project_subscribers[project_id] = [queue]

    # Broadcast a message
    await broadcast_to_project(project_id, "test_event", {"foo": "bar"})

    # Verify the message was formatted for SSE and placed in the queue
    msg = await queue.get()
    assert "data: " in msg
    assert "test_event" in msg
    assert "bar" in msg
    
    # Cleanup
    del project_subscribers[project_id]
