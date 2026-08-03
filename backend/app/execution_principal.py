"""Canonical principal for project-scoped execution resources.

``Task.launched_by_user_id`` is attribution: it records who asked Blueprint to
run the task.  Provider accounts, credentials, runner dispatch, and their
associated limits belong to the project, so those resources always resolve
through ``Project.user_id``.

Keeping that distinction behind one helper prevents a new dispatch path from
quietly reintroducing "launcher pays" semantics.
"""

import uuid
from collections.abc import MutableMapping

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Project


async def resolve_project_execution_principal(
    session: AsyncSession,
    project_id: uuid.UUID | None,
    *,
    project_user_id: str | None = None,
    owner_cache: MutableMapping[uuid.UUID, str] | None = None,
) -> str:
    """Return the project owner whose resources execute project work.

    Callers that already loaded ``Project.user_id`` can pass it directly.
    Dispatch loops may also provide a cycle-local cache to avoid per-task
    project reads.  An absent project resolves to the empty string, preserving
    the existing no-account behavior at each selection boundary.
    """

    if project_user_id is not None:
        return str(project_user_id)
    if project_id is None:
        return ""
    if owner_cache is not None and project_id in owner_cache:
        return str(owner_cache[project_id])

    result = await session.execute(
        select(Project.user_id).where(Project.id == project_id)
    )
    owner = result.scalar_one_or_none()
    principal = str(owner) if owner else ""
    if owner_cache is not None:
        owner_cache[project_id] = principal
    return principal
