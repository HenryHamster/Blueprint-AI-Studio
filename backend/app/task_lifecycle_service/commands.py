"""Command shapes for task lifecycle writes.

These dataclasses describe lifecycle intent. They are intentionally not task
payload schemas; callers may continue to pass open-ended task payload/context
metadata through the service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.models import TaskStatus
from app.task_lifecycle_service.context_patch import ContextPatch


@dataclass(frozen=True)
class CreateTaskCommand:
    """Create a task without constraining task-specific payload/context shape."""

    project_id: Any
    actor: str
    reason: str
    payloads: Sequence[Mapping[str, Any]] = field(default_factory=list)
    title: str = "Untitled Task"
    description: str = ""
    context: Mapping[str, Any] = field(default_factory=dict)
    status: TaskStatus = TaskStatus.QUEUED
    created_by: str = "USER"
    launched_by_user_id: str | None = None
    requires_approval: bool = False
    priority: int = 50
    depends_on: Sequence[str] = ()
    idempotency_key: str | None = None
    task_fields: Mapping[str, Any] = field(default_factory=dict)
    details: Mapping[str, Any] | None = None
    record_history: bool = True


@dataclass(frozen=True)
class SpawnTaskCommand:
    """Spawn a child/follow-up task with open-ended payload/context metadata."""

    project_id: Any
    parent_task_id: Any
    actor: str
    reason: str
    payloads: Sequence[Mapping[str, Any]] = field(default_factory=list)
    title: str = "Untitled Task"
    description: str = ""
    context: Mapping[str, Any] = field(default_factory=dict)
    status: TaskStatus = TaskStatus.QUEUED
    created_by: str = "ORCHESTRATOR"
    requires_approval: bool = False
    priority: int = 50
    depends_on: Sequence[str] = ()
    idempotency_key: str | None = None
    task_fields: Mapping[str, Any] = field(default_factory=dict)
    details: Mapping[str, Any] | None = None
    record_history: bool = True


@dataclass(frozen=True)
class TransitionTaskCommand:
    """Request a status transition plus optional context/message updates."""

    status: TaskStatus
    actor: str
    reason: str
    message: str | None = None
    details: Mapping[str, Any] | None = None
    context_patch: ContextPatch | None = None
    allow_terminal_requeue: bool = False
    record_history: bool = True
