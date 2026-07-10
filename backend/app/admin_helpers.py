"""Shared project/task access-control helpers for admin routers.

Centralizes the membership and role checks reused across the admin router
modules: resolving a project or task while asserting the current user is a
member (raising 404/403 as appropriate) and the role-gated variants that also
enforce a minimum ``viewer``/``member``/``owner`` level. Re-exports
``modify_task_apply`` so router modules import task mutation from one place.
"""

import logging
import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Task, Project, ProjectMember
from app.admin_task_mutations import modify_task_apply as modify_task_apply

logger = logging.getLogger(__name__)

_ROLE_LEVEL = {"viewer": 0, "member": 1, "owner": 2}


def _user_project_ids(user_id: str):
    """Subquery returning project IDs the user is a member of (any role)."""
    return select(ProjectMember.project_id).where(ProjectMember.user_id == user_id)


async def _get_user_project(
    project_id: uuid.UUID, user_id: str, session: AsyncSession
) -> Project:
    """Fetch a project ensuring the user is a member. Raises 404 if not found."""
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    member = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if not member:
        logger.warning(
            "cross_user_project_access_denied project_id=%s user_id=%s owner=%s",
            project_id,
            user_id,
            project.user_id,
        )
        raise HTTPException(status_code=404, detail="Project not found")
    return project


async def _get_user_project_with_role(
    project_id: uuid.UUID, user_id: str, session: AsyncSession, min_role: str = "viewer"
) -> Project:
    """Fetch a project ensuring the user has at least min_role. Raises 403 if insufficient."""
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    member = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Project not found")
    if _ROLE_LEVEL.get(member.role, 0) < _ROLE_LEVEL[min_role]:
        raise HTTPException(
            status_code=403, detail=f"Requires {min_role} role on this project"
        )
    return project


async def _get_user_task(
    task_id: uuid.UUID, user_id: str, session: AsyncSession
) -> Task:
    """Fetch a task ensuring the user is a member of its project. Raises 404 if not found."""
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    member = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == task.project_id,
                ProjectMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


async def _get_user_task_with_role(
    task_id: uuid.UUID, user_id: str, session: AsyncSession, min_role: str = "member"
) -> Task:
    """Fetch a task ensuring the user has at least min_role on its project."""
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    member = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == task.project_id,
                ProjectMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Task not found")
    if _ROLE_LEVEL.get(member.role, 0) < _ROLE_LEVEL[min_role]:
        raise HTTPException(
            status_code=403, detail=f"Requires {min_role} role on this project"
        )
    return task
