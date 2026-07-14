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


def _member_project_ids(user_id: str):
    """Subquery returning project IDs where the user holds at least ``member``.

    The mutation floor: ``viewer`` is documented read-only, so bulk-mutating
    routes must scope to this subquery rather than :func:`_user_project_ids`.
    """
    return select(ProjectMember.project_id).where(
        ProjectMember.user_id == user_id,
        ProjectMember.role.in_(["member", "owner"]),
    )


async def _resolve_repo_project_for_write(
    session: AsyncSession,
    user_id: str,
    github_owner: str,
    github_repo: str,
) -> Project | None:
    """Resolve which project a task-creation call for ``owner/repo`` should use.

    Returns the project when the caller holds member+ on one for this repo, or
    ``None`` when the caller has no project for the repo at all — in which case
    the caller is expected to auto-create one and become its owner.

    Raises 403 when the only project(s) the caller can see for this repo are ones
    where they are a ``viewer``: creating work there is not authorized, and
    falling through to the auto-create branch would silently mint a shadow
    project the viewer owns.

    Ordering is load-bearing. ``ix_project_github_owner_repo`` is NOT unique and
    the auto-create branch deliberately mints a project per (caller, repo), so
    one repo can legitimately map to several projects — a user may be a viewer on
    project A and the owner of project B for the same ``acme/widget``. Picking a
    project by membership alone (any role) and only THEN checking the role would
    turn a benign nondeterministic pick into a nondeterministic 403, randomly
    locking a user out of creating tasks on their OWN project. So we look for a
    member+ project FIRST, and only if none exists do we consider viewer-visible
    ones (and refuse). The explicit ORDER BY makes the pick itself deterministic.
    """
    base = select(Project).where(
        Project.github_owner == github_owner,
        Project.github_repo == github_repo,
    )

    writable = (
        (
            await session.execute(
                base.where(Project.id.in_(_member_project_ids(user_id)))
                .order_by(Project.created_at.asc(), Project.id.asc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if writable is not None:
        return writable

    viewer_visible = (
        (
            await session.execute(
                base.where(Project.id.in_(_user_project_ids(user_id)))
                .order_by(Project.created_at.asc(), Project.id.asc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if viewer_visible is not None:
        raise HTTPException(
            status_code=403, detail="Requires member role on this project"
        )

    return None


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
