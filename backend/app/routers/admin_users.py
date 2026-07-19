"""User and project member management endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import get_session
from app.models import AllowedUser, ProjectMember
from app.auth import get_current_user, require_admin
from app.admin import _get_user_project, _get_user_project_with_role
from app.task_locking import lock_project_or_429

router = APIRouter(
    prefix="/api/admin", tags=["admin-users"], dependencies=[Depends(get_current_user)]
)


# ── Pydantic Models ───────────────────────────────────────────────────────────


class _MemberCreate(BaseModel):
    email: str
    role: str = "member"

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in ("member", "viewer"):
            raise ValueError("Role must be 'member' or 'viewer'")
        return v


class _MemberUpdate(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in ("member", "viewer"):
            raise ValueError("Role must be 'member' or 'viewer'")
        return v


class _UserCreate(BaseModel):
    email: str
    role: str = "user"

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v:
            raise ValueError("Invalid email address")
        return v

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
        select(ProjectMember, AllowedUser.email)
        .outerjoin(AllowedUser, AllowedUser.firebase_uid == ProjectMember.user_id)
        .where(ProjectMember.project_id == project_id)
        .order_by(ProjectMember.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    rows = result.all()
    return {
        "members": [
            {
                "id": str(m.id),
                "user_id": m.user_id,
                "email": email,
                "role": m.role,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m, email in rows
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
    await lock_project_or_429(session, project_id)

    # Look up user by email in AllowedUser table
    allowed = (
        await session.execute(
            select(AllowedUser).where(
                AllowedUser.email == payload.email.strip().lower()
            )
        )
    ).scalar_one_or_none()
    if not allowed:
        raise HTTPException(
            status_code=404,
            detail="User not found. They must be added to the system first.",
        )
    target_user_id = allowed.firebase_uid
    if not target_user_id:
        raise HTTPException(
            status_code=400,
            detail="User has not signed in yet (no Firebase UID linked)",
        )

    # Check if already a member
    existing = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == target_user_id,
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409, detail="User is already a member of this project"
        )

    member = ProjectMember(
        project_id=project_id, user_id=target_user_id, role=payload.role
    )
    session.add(member)
    await session.commit()
    await session.refresh(member)
    return {
        "id": str(member.id),
        "user_id": member.user_id,
        "email": allowed.email,
        "role": member.role,
        "created_at": member.created_at.isoformat() if member.created_at else None,
    }


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
    return {"id": str(member.id), "user_id": member.user_id, "role": member.role}


@router.delete("/projects/{project_id}/members/{member_id}")
async def remove_project_member(
    project_id: uuid.UUID,
    member_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Remove a member from a project. Owner-only (except members can remove themselves)."""
    member = await session.get(ProjectMember, member_id)
    if not member or member.project_id != project_id:
        raise HTTPException(status_code=404, detail="Member not found")

    # Owner cannot be removed
    if member.role == "owner":
        raise HTTPException(status_code=400, detail="Cannot remove the project owner")

    # Non-owners can only remove themselves (leave)
    if member.user_id != current_user["user_id"]:
        await _get_user_project_with_role(
            project_id, current_user["user_id"], session, "owner"
        )

    await lock_project_or_429(session, project_id)
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
                "last_login_at": u.last_login_at.isoformat()
                if u.last_login_at
                else None,
                "created_at": u.created_at.isoformat() if u.created_at else None,
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
    existing = (
        await session.execute(
            select(AllowedUser).where(AllowedUser.email == payload.email)
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409, detail="User with this email already exists"
        )

    user = AllowedUser(email=payload.email, role=payload.role)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return {
        "id": str(user.id),
        "email": user.email,
        "role": user.role,
        "display_name": user.display_name,
        "last_login_at": None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
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

    user.role = payload.role
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return {
        "id": str(user.id),
        "email": user.email,
        "role": user.role,
        "display_name": user.display_name,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
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
    ) or user.email == current_user.get("email"):
        raise HTTPException(status_code=400, detail="Cannot remove your own access")

    await session.delete(user)
    await session.commit()
    return {"message": f"User {user.email} removed"}
