"""Central task lifecycle write helpers.

The lifecycle service owns mutation mechanics: row locking, JSONB patching,
status transitions, and task history entries. It deliberately does not define
task payload schemas or allowed context keys.
"""

from app.task_lifecycle_service.commands import (
    CreateTaskCommand,
    SpawnTaskCommand,
    TransitionTaskCommand,
)
from app.task_lifecycle_service.context_patch import ContextPatch, apply_context_patch
from app.task_lifecycle_service.service import (
    InvalidTaskTransition,
    TaskLifecycleService,
)
from app.task_lifecycle_service.transitions import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    is_active_status,
    is_terminal_status,
)

__all__ = [
    "ACTIVE_STATUSES",
    "TERMINAL_STATUSES",
    "ContextPatch",
    "CreateTaskCommand",
    "InvalidTaskTransition",
    "SpawnTaskCommand",
    "TaskLifecycleService",
    "TransitionTaskCommand",
    "apply_context_patch",
    "is_active_status",
    "is_terminal_status",
]
