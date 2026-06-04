"""Realtime websocket events for strategist STATE & OUTPUTS.

Centralizes the three event-name contracts the admin UI depends on so every
backend change site emits a consistent trigger-a-refetch signal. Payloads are
intentionally tiny (ids / labels only) — the frontend refetches full state over
HTTP, so every emission stays well under the ~7KB cross-instance NOTIFY ceiling
enforced by event_fanout.py.

All emitters MUST be awaited AFTER the DB commit, never while holding a
directions/project lock, mirroring the established ``task_updated`` /
``alert_created`` post-commit broadcast convention. ``broadcast_to_project``
swallows its own exceptions, so a failed emit never escapes into the calling
HTTP handler after its commit.

These event names are a contract with the frontend — do not rename without a
coordinated admin-ui change.
"""

from app.broadcast import broadcast_to_project

# Event-name contract shared with the admin UI.
STRATEGIC_CYCLE_STARTED = "strategic_cycle_started"
STRATEGIC_CYCLE_COMPLETED = "strategic_cycle_completed"
STRATEGIC_UPDATED = "strategic_updated"

# Allowed ``kind`` values for a ``strategic_updated`` event. Scopes the signal
# to the strategist's persisted OUTPUTS surfaced in the directions view.
STRATEGIC_KIND_DIRECTIONS = "directions"
STRATEGIC_KIND_RUNNING_UPDATE = "running_update"
STRATEGIC_KIND_PROBLEM_MODEL = "problem_model"


async def emit_strategic_cycle_started(project_id, task_id, phase) -> None:
    """Broadcast that a strategic cycle task became active (post-commit)."""
    await broadcast_to_project(
        str(project_id),
        STRATEGIC_CYCLE_STARTED,
        {"task_id": str(task_id), "phase": phase},
    )


async def emit_strategic_cycle_completed(project_id, task_id, phase) -> None:
    """Broadcast that a strategic cycle finished — success OR failure (post-commit)."""
    await broadcast_to_project(
        str(project_id),
        STRATEGIC_CYCLE_COMPLETED,
        {"task_id": str(task_id), "phase": phase},
    )


async def emit_strategic_updated(project_id, kind) -> None:
    """Broadcast that a strategist output changed (post-commit).

    ``kind`` is one of ``directions`` / ``running_update`` / ``problem_model``.
    """
    await broadcast_to_project(
        str(project_id),
        STRATEGIC_UPDATED,
        {"kind": kind},
    )
