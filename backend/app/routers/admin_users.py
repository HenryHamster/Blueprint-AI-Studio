"""User and project member management endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import get_session
from app.models import AllowedUser, ProjectMember
from app.auth import get_current_user, require_admin
from app.admin_helpers import (
    PROJECT_NON_OWNER_ROLES,
    _get_user_project,
    _get_user_project_role,
    _get_user_project_with_role,
    _require_project_role,
    parse_pending_project_member_user_id,
    pending_project_member_user_id,
    strongest_project_role,
)
from app.task_locking import lock_project_or_429
from app.allowed_users import (
    AllowedUserEmailConflict,
    normalize_allowed_user_email,
    resolve_allowed_user_by_email,
)
from app.time_utils import utc_iso

router = APIRouter(
    prefix="/api/admin", tags=["admin-users"], dependencies=[Depends(get_current_user)]
)


# ── Pydantic Models ───────────────────────────────────────────────────────────


def _normalize_email(value: str) -> str:
    return normalize_allowed_user_email(value)


class _MemberCreate(BaseModel):
    email: str
    role: str = "member"

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        return _normalize_email(v)

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in PROJECT_NON_OWNER_ROLES:
            raise ValueError("Role must be 'member' or 'viewer'")
        return v


class _MemberUpdate(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in PROJECT_NON_OWNER_ROLES:
            raise ValueError("Role must be 'member' or 'viewer'")
        return v


class _UserCreate(BaseModel):
    email: str
    role: str = "user"

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        return _normalize_email(v)

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in ("admin", "user"):
            raise ValueError("Role must be 'admin' or 'user'")
        return v


class _UserUpdate(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in ("admin", "user"):
            raise ValueError("Role must be 'admin' or 'user'")
        return v


# ── Project Member Endpoints ──────────────────────────────────────────────────


async def _allowed_user_for_member(
    session: AsyncSession, member: ProjectMember
) -> tuple[AllowedUser | None, bool]:
    pending_allowed_user_id = parse_pending_project_member_user_id(member.user_id)
    if pending_allowed_user_id is not None:
        return await session.get(AllowedUser, pending_allowed_user_id), True
    allowed = (
        await session.execute(
            select(AllowedUser).where(AllowedUser.firebase_uid == member.user_id)
        )
    ).scalars().first()
    return allowed, False


def _member_response(
    member: ProjectMember,
    allowed_user: AllowedUser | None,
    *,
    pending: bool,
) -> dict:
    return {
        "id": str(member.id),
        # Preserve the established active-member field while never exposing the
        # synthetic allowed-user:<uuid> identity used before first login.
        "user_id": None if pending else member.user_id,
        "email": allowed_user.email if allowed_user else None,
        "role": member.role,
        "status": "pending" if pending else "active",
        "created_at": utc_iso(member.created_at),
    }


async def _get_or_create_allowed_user(
    session: AsyncSession, normalized_email: str
) -> AllowedUser:
    try:
        allowed = await resolve_allowed_user_by_email(
            session, normalized_email, for_update=True
        )
    except AllowedUserEmailConflict:
        raise HTTPException(
            status_code=409,
            detail="Conflicting allowlist records exist for this email",
        ) from None
    if allowed is not None:
        return allowed

    candidate = AllowedUser(email=normalized_email, role="invited")
    try:
        # The savepoint keeps the owner request transaction usable if another
        # project invitation creates the same globally-unique email first.
        async with session.begin_nested():
            session.add(candidate)
            await session.flush()
        return candidate
    except IntegrityError:
        try:
            allowed = await resolve_allowed_user_by_email(
                session, normalized_email, for_update=True
            )
        except AllowedUserEmailConflict:
            raise HTTPException(
                status_code=409,
                detail="Conflicting allowlist records exist for this email",
            ) from None
        if allowed is None:
            raise
        return allowed


@router.get("/projects/{project_id}/members")
async def list_project_members(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """List members of a project. Any member/viewer can see."""
    await _get_user_project(project_id, current_user["user_id"], session)
    total_result = await session.execute(
        select(func.count())
        .select_from(ProjectMember)
        .where(ProjectMember.project_id == project_id)
    )
    total = total_result.scalar_one()

    result = await session.execute(
        select(ProjectMember)
        .where(ProjectMember.project_id == project_id)
        .order_by(ProjectMember.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    members = list(result.scalars().all())
    pending_ids = {
        pending_id
        for member in members
        if (pending_id := parse_pending_project_member_user_id(member.user_id))
        is not None
    }
    active_uids = {
        member.user_id
        for member in members
        if parse_pending_project_member_user_id(member.user_id) is None
    }
    allowed_users = []
    if pending_ids or active_uids:
        allowed_users = list(
            (
                await session.execute(
                    select(AllowedUser).where(
                        or_(
                            AllowedUser.id.in_(pending_ids),
                            AllowedUser.firebase_uid.in_(active_uids),
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
    by_id = {allowed.id: allowed for allowed in allowed_users}
    by_uid = {
        allowed.firebase_uid: allowed
        for allowed in allowed_users
        if allowed.firebase_uid is not None
    }
    return {
        "members": [
            _member_response(
                member,
                by_id.get(pending_id) if pending_id else by_uid.get(member.user_id),
                pending=pending_id is not None,
            )
            for member in members
            for pending_id in [parse_pending_project_member_user_id(member.user_id)]
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/projects/{project_id}/members", status_code=201)
async def add_project_member(
    project_id: uuid.UUID,
    payload: _MemberCreate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Add a member to a project. Owner-only."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    allowed = await _get_or_create_allowed_user(session, payload.email)
    pending_user_id = pending_project_member_user_id(allowed.id)
    target_user_id = allowed.firebase_uid or pending_user_id

    identities = {target_user_id, pending_user_id}
    existing_members = list(
        (
            await session.execute(
                select(ProjectMember)
                .where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id.in_(identities),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if existing_members:
        member = next(
            (item for item in existing_members if item.user_id == target_user_id),
            existing_members[0],
        )
        member.user_id = target_user_id
        member.role = strongest_project_role(
            payload.role, *(item.role for item in existing_members)
        )
        member.created_at = min(item.created_at for item in existing_members)
        session.add(member)
        for duplicate in existing_members:
            if duplicate.id != member.id:
                await session.delete(duplicate)
        await session.commit()
        return _member_response(
            member, allowed, pending=allowed.firebase_uid is None
        )

    member = ProjectMember(
        project_id=project_id, user_id=target_user_id, role=payload.role
    )
    try:
        async with session.begin_nested():
            session.add(member)
            await session.flush()
    except IntegrityError:
        # A concurrent identical invitation won the unique project/user key.
        member = (
            await session.execute(
                select(ProjectMember)
                .where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == target_user_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        member.role = strongest_project_role(member.role, payload.role)
        session.add(member)

    await session.commit()
    return _member_response(
        member, allowed, pending=allowed.firebase_uid is None
    )


@router.patch("/projects/{project_id}/members/{member_id}")
async def update_project_member(
    project_id: uuid.UUID,
    member_id: uuid.UUID,
    payload: _MemberUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Update a member's role. Owner-only. Cannot change owner's own role."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    await lock_project_or_429(session, project_id)

    member = (
        await session.execute(
            select(ProjectMember)
            .where(ProjectMember.id == member_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not member or member.project_id != project_id:
        raise HTTPException(status_code=404, detail="Member not found")
    if member.role == "owner":
        raise HTTPException(status_code=400, detail="Cannot change the owner's role")

    member.role = payload.role
    session.add(member)
    await session.commit()
    allowed, pending = await _allowed_user_for_member(session, member)
    return _member_response(member, allowed, pending=pending)


@router.delete("/projects/{project_id}/members/{member_id}")
async def remove_project_member(
    project_id: uuid.UUID,
    member_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Remove a member from a project. Owner-only (except members can remove themselves)."""
    actor_role = await _get_user_project_role(
        project_id, current_user["user_id"], session
    )
    member = await session.get(ProjectMember, member_id)
    if not member or member.project_id != project_id:
        raise HTTPException(status_code=404, detail="Member not found")

    # Owner cannot be removed
    if member.role == "owner":
        raise HTTPException(status_code=400, detail="Cannot remove the project owner")

    # Non-owners can only remove themselves (leave)
    if member.user_id != current_user["user_id"]:
        _require_project_role(actor_role, "owner")

    await lock_project_or_429(session, project_id)
    pending_allowed_user_id = parse_pending_project_member_user_id(member.user_id)
    pending_allowed_user = None
    if pending_allowed_user_id is not None:
        pending_allowed_user = (
            await session.execute(
                select(AllowedUser)
                .where(AllowedUser.id == pending_allowed_user_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
    member = (
        await session.execute(
            select(ProjectMember)
            .where(
                ProjectMember.id == member_id,
                ProjectMember.project_id == project_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    if member.role == "owner":
        raise HTTPException(status_code=400, detail="Cannot remove the project owner")

    await session.delete(member)
    await session.flush()
    if pending_allowed_user is not None:
        pending_user_id = pending_project_member_user_id(pending_allowed_user.id)
        remaining = (
            await session.execute(
                select(func.count())
                .select_from(ProjectMember)
                .where(ProjectMember.user_id == pending_user_id)
            )
        ).scalar_one()
        if (
            remaining == 0
            and pending_allowed_user.firebase_uid is None
            and pending_allowed_user.role == "invited"
        ):
            await session.delete(pending_allowed_user)
    await session.commit()
    return {"message": "Member removed"}


# ── User Management Endpoints ─────────────────────────────────────────────────


@router.get("/users")
async def list_users(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    total_result = await session.execute(select(func.count()).select_from(AllowedUser))
    total = total_result.scalar_one()

    if offset > total:
        raise HTTPException(
            status_code=400,
            detail=f"offset {offset} exceeds total user count {total}",
        )

    result = await session.execute(
        select(AllowedUser)
        .order_by(AllowedUser.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    users = result.scalars().all()
    return {
        "users": [
            {
                "id": str(u.id),
                "email": u.email,
                "role": u.role,
                "display_name": u.display_name,
                "last_login_at": utc_iso(u.last_login_at),
                "created_at": utc_iso(u.created_at),
            }
            for u in users
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/users", status_code=201)
async def add_user(
    payload: _UserCreate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    try:
        existing = await resolve_allowed_user_by_email(session, payload.email)
    except AllowedUserEmailConflict:
        raise HTTPException(
            status_code=409,
            detail="Conflicting allowlist records exist for this email",
        ) from None
    if existing:
        raise HTTPException(
            status_code=409, detail="User with this email already exists"
        )

    user = AllowedUser(email=payload.email, role=payload.role)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="User with this email already exists"
        ) from None
    await session.refresh(user)
    return {
        "id": str(user.id),
        "email": user.email,
        "role": user.role,
        "display_name": user.display_name,
        "last_login_at": None,
        "created_at": utc_iso(user.created_at),
    }


@router.patch("/users/{user_id}")
async def update_user(
    user_id: uuid.UUID,
    payload: _UserUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    user = await session.get(AllowedUser, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    normalized_email = normalize_allowed_user_email(user.email)
    conflicts = list(
        (
            await session.execute(
                select(AllowedUser).where(
                    func.lower(AllowedUser.email) == normalized_email,
                    AllowedUser.id != user.id,
                )
            )
        )
        .scalars()
        .all()
    )
    if conflicts:
        raise HTTPException(
            status_code=409,
            detail="Conflicting allowlist records exist for this email",
        )
    user.email = normalized_email
    user.role = payload.role
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return {
        "id": str(user.id),
        "email": user.email,
        "role": user.role,
        "display_name": user.display_name,
        "last_login_at": utc_iso(user.last_login_at),
        "created_at": utc_iso(user.created_at),
    }


@router.delete("/users/{user_id}")
async def remove_user(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    user = await session.get(AllowedUser, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Prevent self-deletion
    if user.firebase_uid == current_user.get(
        "user_id"
    ) or normalize_allowed_user_email(user.email) == current_user.get("email"):
        raise HTTPException(status_code=400, detail="Cannot remove your own access")

    user = (
        await session.execute(
            select(AllowedUser)
            .where(AllowedUser.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    pending_user_id = pending_project_member_user_id(user.id)
    pending_members = list(
        (
            await session.execute(
                select(ProjectMember)
                .where(ProjectMember.user_id == pending_user_id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if any(member.role == "owner" for member in pending_members):
        raise HTTPException(status_code=400, detail="Cannot remove a project owner")
    for member in pending_members:
        await session.delete(member)
    await session.delete(user)
    await session.commit()
    return {"message": f"User {user.email} removed"}
