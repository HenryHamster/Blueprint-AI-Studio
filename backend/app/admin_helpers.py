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

from app.models import AllowedUser, Task, Project, ProjectMember
from app.admin_task_mutations import modify_task_apply as modify_task_apply

logger = logging.getLogger(__name__)

# Canonical project-role vocabulary.  Keep this small and explicit: project
# roles are persisted strings today, and accepting an unknown value as the
# lowest known role would turn a corrupt/future role into viewer access.  All
# authorization comparisons route through :func:`_project_role_allows` so an
# unrecognized value fails closed.
PROJECT_VIEW_ROLES = ("viewer", "member", "owner")
PROJECT_WRITE_ROLES = ("member", "owner")
PROJECT_NON_OWNER_ROLES = ("viewer", "member")
_ROLE_LEVEL = {role: level for level, role in enumerate(PROJECT_VIEW_ROLES)}
PENDING_PROJECT_MEMBER_PREFIX = "allowed-user:"


class AllowedUserBoundToDifferentUID(RuntimeError):
    """A concurrent first-login claim established a different Firebase UID."""


def pending_project_member_user_id(allowed_user_id: uuid.UUID) -> str:
    """Return the durable pre-login ProjectMember identity for an AllowedUser."""

    return f"{PENDING_PROJECT_MEMBER_PREFIX}{allowed_user_id}"


def parse_pending_project_member_user_id(user_id: str) -> uuid.UUID | None:
    """Resolve a synthetic pending identity without exposing it through the API."""

    if not user_id.startswith(PENDING_PROJECT_MEMBER_PREFIX):
        return None
    try:
        return uuid.UUID(user_id.removeprefix(PENDING_PROJECT_MEMBER_PREFIX))
    except ValueError:
        return None


def strongest_project_role(*roles: str) -> str:
    """Choose the strongest canonical role; persisted invalid roles fail closed."""

    canonical = [role for role in roles if role in _ROLE_LEVEL]
    if not canonical:
        raise ValueError("No canonical project role supplied")
    return max(canonical, key=_ROLE_LEVEL.__getitem__)


async def claim_pending_project_memberships(
    session: AsyncSession,
    allowed_user_id: uuid.UUID,
    firebase_uid: str,
) -> AllowedUser:
    """Atomically link an AllowedUser and claim/merge all email invitations.

    The AllowedUser row is the concurrency serializer for first login. A second
    verified login waits, observes the completed link, and performs no duplicate
    membership writes. Existing real memberships win identity while the
    strongest role across real and pending rows is retained.
    """

    allowed_user = (
        await session.execute(
            select(AllowedUser)
            .where(AllowedUser.id == allowed_user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if allowed_user.firebase_uid not in (None, firebase_uid):
        # Preserve the established account binding. The caller authenticated by
        # normalized email, but changing an existing UID is account recovery,
        # not invitation claiming.
        raise AllowedUserBoundToDifferentUID(
            "AllowedUser was concurrently bound to a different Firebase UID"
        )

    pending_user_id = pending_project_member_user_id(allowed_user.id)
    pending_members = list(
        (
            await session.execute(
                select(ProjectMember)
                .where(ProjectMember.user_id == pending_user_id)
                .order_by(ProjectMember.project_id.asc())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for pending in pending_members:
        real = (
            await session.execute(
                select(ProjectMember)
                .where(
                    ProjectMember.project_id == pending.project_id,
                    ProjectMember.user_id == firebase_uid,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if real is None:
            pending.user_id = firebase_uid
            session.add(pending)
            continue
        real.role = strongest_project_role(real.role, pending.role)
        real.created_at = min(real.created_at, pending.created_at)
        session.add(real)
        await session.delete(pending)

    allowed_user.firebase_uid = firebase_uid
    if allowed_user.role == "invited":
        allowed_user.role = "user"
    session.add(allowed_user)
    return allowed_user


def _project_role_allows(role: object, min_role: str) -> bool:
    """Return whether *role* satisfies *min_role*, failing closed on bad data.

    ``min_role`` is developer-authored policy, so an unknown minimum is a
    programming error rather than an authorization denial.  ``role`` comes
    from persisted membership data and may be corrupt during a partial deploy
    or manual repair; unknown values must never inherit viewer access.
    """

    if min_role not in _ROLE_LEVEL:
        raise ValueError(f"Unknown minimum project role: {min_role!r}")
    if not isinstance(role, str) or role not in _ROLE_LEVEL:
        return False
    return _ROLE_LEVEL[role] >= _ROLE_LEVEL[min_role]


def _require_project_role(role: object, min_role: str) -> None:
    """Raise the canonical 403 when *role* is below the required floor."""

    if not _project_role_allows(role, min_role):
        raise HTTPException(
            status_code=403, detail=f"Requires {min_role} role on this project"
        )


def _user_project_ids(user_id: str):
    """Subquery returning project IDs visible through a recognized role."""
    return select(ProjectMember.project_id).where(
        ProjectMember.user_id == user_id,
        ProjectMember.role.in_(PROJECT_VIEW_ROLES),
    )


def _member_project_ids(user_id: str):
    """Subquery returning project IDs where the user holds at least ``member``.

    The mutation floor: ``viewer`` is documented read-only, so bulk-mutating
    routes must scope to this subquery rather than :func:`_user_project_ids`.
    """
    return select(ProjectMember.project_id).where(
        ProjectMember.user_id == user_id,
        ProjectMember.role.in_(PROJECT_WRITE_ROLES),
    )


async def _get_user_project_role(
    project_id: uuid.UUID, user_id: str, session: AsyncSession
) -> str:
    """Return the caller's validated role, hiding absent membership with 404.

    A present membership with an unknown role is denied with the same role-floor
    403 used elsewhere.  This is deliberately distinct from no membership: the
    latter remains a 404 tenant-isolation boundary.
    """

    member = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not _project_role_allows(member.role, "viewer"):
        logger.error(
            "invalid_project_role_denied project_id=%s user_id=%s role=%r",
            project_id,
            user_id,
            member.role,
        )
        _require_project_role(member.role, "viewer")
    return member.role


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
    try:
        await _get_user_project_role(project_id, user_id, session)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        logger.warning(
            "cross_user_project_access_denied project_id=%s user_id=%s owner=%s",
            project_id,
            user_id,
            project.user_id,
        )
        raise
    return project


async def _get_user_project_with_role(
    project_id: uuid.UUID, user_id: str, session: AsyncSession, min_role: str = "viewer"
) -> Project:
    """Fetch a project ensuring the user has at least min_role. Raises 403 if insufficient."""
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    role = await _get_user_project_role(project_id, user_id, session)
    _require_project_role(role, min_role)
    return project


async def _get_user_task(
    task_id: uuid.UUID, user_id: str, session: AsyncSession
) -> Task:
    """Fetch a task ensuring the user is a member of its project. Raises 404 if not found."""
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        await _get_user_project_role(task.project_id, user_id, session)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="Task not found") from None
        raise
    return task


async def _get_user_task_with_role(
    task_id: uuid.UUID, user_id: str, session: AsyncSession, min_role: str = "member"
) -> Task:
    """Fetch a task ensuring the user has at least min_role on its project."""
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        role = await _get_user_project_role(task.project_id, user_id, session)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="Task not found") from None
        raise
    _require_project_role(role, min_role)
    return task
