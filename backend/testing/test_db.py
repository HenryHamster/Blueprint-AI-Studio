import pytest
from app.models import Project, Task, Artifact, ProjectStatus, TaskStatus, ArtifactType

@pytest.mark.asyncio
async def test_project_creation(db_session):
    """
    Test that a Project is created with the correct default status.
    """
    project = Project(name="Test DB Project", user_id="user1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    assert project.id is not None
    assert project.status == ProjectStatus.DEPLOYING

@pytest.mark.asyncio
async def test_task_cascading(db_session):
    """
    Test that a Task correctly links to a Project.
    """
    project = Project(name="Test DB Project", user_id="user1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{"cmd": "ls"}], vm_instance_id="vm-1")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    assert task.project_id == project.id
    assert task.status == TaskStatus.QUEUED

@pytest.mark.asyncio
async def test_artifact_linking(db_session):
    """
    Test that an Artifact correctly links to a Task and stores metadata.
    """
    project = Project(name="Test DB Project", user_id="user1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(project_id=project.id, payloads=[{"cmd": "ls"}], vm_instance_id="vm-1")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    artifact = Artifact(task_id=task.id, artifact_type=ArtifactType.ERROR_LOG, logs="test logs")
    db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(artifact)

    assert artifact.task_id == task.id
    assert artifact.logs == "test logs"
    assert artifact.artifact_type == ArtifactType.ERROR_LOG
