"""Visibility rules for operator-facing task work surfaces.

Continuous Workday Resident generations are durable process/audit records, not
independent units of project work.  Ordinary task and pipeline projections hide
them while direct task lookup and explicit audit queries retain the raw rows.
"""

from sqlalchemy import and_

from app.models import Task


RESIDENT_INFRASTRUCTURE_CONTEXT_KEY = "_resident_infrastructure"
LEGACY_RESIDENT_SESSION_CONTEXT_KEY = "_resident_session"


def non_resident_infrastructure_task_filter():
    """Return the portable SQL predicate for ordinary task work surfaces.

    The legacy Resident marker remains part of the exclusion during rollout so
    generations created before the explicit infrastructure marker landed do not
    reappear in the pipeline. ``IS NOT TRUE`` includes missing/null JSON keys on
    both PostgreSQL and SQLite.
    """

    return and_(
        Task.context[RESIDENT_INFRASTRUCTURE_CONTEXT_KEY].as_boolean().is_not(True),
        Task.context[LEGACY_RESIDENT_SESSION_CONTEXT_KEY].as_boolean().is_not(True),
    )


def is_resident_infrastructure_task(task: Task) -> bool:
    """Classify an already-loaded Task without relying on its payload type."""

    context = task.context if isinstance(task.context, dict) else {}
    return bool(
        context.get(RESIDENT_INFRASTRUCTURE_CONTEXT_KEY) is True
        or context.get(LEGACY_RESIDENT_SESSION_CONTEXT_KEY) is True
    )
