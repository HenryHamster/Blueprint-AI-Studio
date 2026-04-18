"""Test factories for creating model instances with sensible defaults."""
import uuid
from datetime import datetime, timezone

from app.models import Alert, Task, TaskHistory, Project, ProjectMember, TaskStatus


def _utcnow() -> datetime:
    """Naive UTC timestamp matching the model's internal helper."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def make_project(**kwargs) -> Project:
    """Create a Project instance with test defaults. Pass kwargs to override."""
    defaults = dict(
        name="Test Project",
        user_id="test-user-123",
        autonomous_enabled=False,
        directions=[],
    )
    defaults.update(kwargs)
    return Project(**defaults)


def make_membership(project_id, user_id="test-user-123", role="owner") -> ProjectMember:
    """Create a ProjectMember (owner by default) for test projects."""
    return ProjectMember(project_id=project_id, user_id=user_id, role=role)


def make_task(project_id=None, **kwargs) -> Task:
    """Create a Task instance with test defaults. Pass kwargs to override."""
    if project_id is None:
        project_id = uuid.uuid4()
    defaults = dict(
        project_id=project_id,
        title="Test Task",
        description="A test task",
        status=TaskStatus.QUEUED,
        created_by="test-user",
        requires_approval=False,
        payloads=[],
        context={},
        status_log=[],
    )
    defaults.update(kwargs)
    # When tests backdate created_at, mirror it to updated_at unless the test
    # explicitly sets updated_at. Without this, updated_at would default to
    # _utcnow() and pollute terminal-updated-in-window filters that treat
    # updated_at as "last touched" (e.g. admin_analytics._rolling_window_stats).
    if "created_at" in kwargs and "updated_at" not in kwargs:
        defaults["updated_at"] = kwargs["created_at"]
    return Task(**defaults)


def make_alert(project_id: uuid.UUID, task_id: uuid.UUID | None = None, **kwargs) -> Alert:
    defaults: dict = dict(
        project_id=project_id,
        task_id=task_id,
        severity="ERROR",
        title="Test alert",
        message="Test alert",
        dismissed_at=None,
        created_at=_utcnow(),
    )
    defaults.update(kwargs)
    return Alert(**defaults)


def make_task_history(task_id: uuid.UUID, **kwargs) -> TaskHistory:
    defaults: dict = dict(
        task_id=task_id,
        action="status_change",
        details={},
        timestamp=_utcnow(),
    )
    defaults.update(kwargs)
    return TaskHistory(**defaults)
