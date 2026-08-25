"""Admin management endpoints for general service credentials (machine tokens).

Minting/listing/revoking a ``ServiceCredential`` is platform-admin-only —
this is the control surface, distinct from the credential's own runtime
authentication path (``app.service_credentials.authenticate_service_token``,
wired into ``app.auth.get_current_user``), which never requires admin.
"""

import re
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.admin_helpers import PROJECT_NON_OWNER_ROLES, strongest_project_role
from app.auth import require_admin
from app.database import get_session
from app.models import Project, ProjectMember, ServiceCredential, _utcnow
from app.service_credentials import SCOPE_DEFINITIONS, hash_service_token, mint_service_token
from app.time_utils import utc_iso

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-service-credentials"],
    dependencies=[Depends(require_admin)],
)

_NAME_PATTERN = re.compile(r"^[a-z0-9-]{1,64}$")
_CREDENTIAL_OWNED_PRINCIPAL_SCOPES = frozenset(
    {"coding-agent:read", "project-operator"}
)


# ── Request models ──────────────────────────────────────────────────────────


class _ProjectMembershipIn(BaseModel):
    project_id: uuid.UUID
    role: str = "viewer"

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        # "owner" is deliberately excluded: a machine principal should never
        # hold full project control, only the read/write floor the caller
        # explicitly grants.
        if v not in PROJECT_NON_OWNER_ROLES:
            raise ValueError("Role must be 'member' or 'viewer'")
        return v


class _ServiceCredentialCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=255)
    scopes: list[str]
    expires_in_days: int = Field(default=90, ge=1, le=365)
    principal_user_id: str | None = None
    project_memberships: list[_ProjectMembershipIn] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not _NAME_PATTERN.match(v):
            raise ValueError("name must be 1-64 chars of [a-z0-9-]")
        return v

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("At least one scope is required")
        unknown = [scope for scope in v if scope not in SCOPE_DEFINITIONS]
        if unknown:
            raise ValueError(f"Unknown scope(s): {', '.join(unknown)}")
        return v


# ── Response helpers ────────────────────────────────────────────────────────


def _credential_public_dict(cred: ServiceCredential) -> dict:
    """Serialize a credential for API responses. Never includes token/hash."""
    return {
        "id": str(cred.id),
        "name": cred.name,
        "description": cred.description,
        "principal_user_id": cred.principal_user_id,
        "token_prefix": cred.token_prefix,
        "scopes": cred.scopes,
        "expires_at": utc_iso(cred.expires_at),
        "revoked_at": utc_iso(cred.revoked_at),
        "created_by": cred.created_by,
        "created_at": utc_iso(cred.created_at),
        "last_used_at": utc_iso(cred.last_used_at),
        "last_used_path": cred.last_used_path,
    }


async def _upsert_service_principal_membership(
    session: AsyncSession, project_id: uuid.UUID, principal_user_id: str, role: str
) -> None:
    """Idempotently grant *role* on *project_id* to *principal_user_id*.

    Mirrors the strongest-role-wins upsert in admin_users.add_project_member:
    minting a second credential (or reusing a principal) against a project the
    principal is already on must not downgrade its existing role, and the
    unique ``(project_id, user_id)`` index means a plain insert can race a
    concurrent mint.
    """
    existing = (
        await session.execute(
            select(ProjectMember)
            .where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == principal_user_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.role = strongest_project_role(existing.role, role)
        session.add(existing)
        return

    member = ProjectMember(project_id=project_id, user_id=principal_user_id, role=role)
    try:
        async with session.begin_nested():
            session.add(member)
            await session.flush()
    except IntegrityError:
        # A concurrent mint for the same principal/project won the unique
        # (project_id, user_id) race.
        member = (
            await session.execute(
                select(ProjectMember)
                .where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == principal_user_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        member.role = strongest_project_role(member.role, role)
        session.add(member)


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post("/service-credentials", status_code=status.HTTP_201_CREATED)
async def mint_service_credential(
    payload: _ServiceCredentialCreate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Mint a new service credential and return its plaintext token once.

    The plaintext token is included ONLY in this response and is not
    retrievable again — only its SHA256 hash is persisted on the row.
    """
    existing = (
        await session.execute(
            select(ServiceCredential).where(
                ServiceCredential.name == payload.name,
                ServiceCredential.revoked_at.is_(None),
            )
        )
    ).scalars().first()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Service credential name '{payload.name}' is already in use",
        )

    # Validate every target project up front so a bad id 404s before any
    # credential/membership state is created.
    for membership in payload.project_memberships:
        project = await session.get(Project, membership.project_id)
        if project is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project {membership.project_id} not found",
            )

    credential_owned_principal = bool(
        _CREDENTIAL_OWNED_PRINCIPAL_SCOPES.intersection(payload.scopes)
    )
    if credential_owned_principal and payload.principal_user_id is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "coding-agent and project-operator credentials require a "
                "credential-owned service "
                "principal; principal_user_id must be omitted"
            ),
        )

    # Credential-owned scoped profiles receive a fresh identity on every mint,
    # including a re-mint of a revoked name. Reusing service:{name} would retain the old
    # principal's ProjectMember rows and could silently preserve projects that
    # the new grant did not name. Other existing service consumers retain the
    # backward-compatible principal convention.
    credential_id = uuid.uuid4()
    principal_user_id = (
        f"service:credential:{credential_id}"
        if credential_owned_principal
        else (payload.principal_user_id or f"service:{payload.name}")
    )

    # A caller-supplied principal_user_id must never reference an existing
    # project owner: the credential authenticates AS that principal, so it
    # would inherit the owner's ProjectMember rows (owner role) and bypass the
    # no-owner membership rule enforced above. The default service:{name}
    # principal is a fresh non-human identity that cannot collide with an owner.
    if payload.principal_user_id is not None:
        owner_membership = (
            await session.execute(
                select(ProjectMember.id)
                .where(
                    ProjectMember.user_id == principal_user_id,
                    ProjectMember.role == "owner",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        owner_project = (
            await session.execute(
                select(Project.id)
                .where(Project.user_id == principal_user_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if owner_membership is not None or owner_project is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="principal_user_id must not reference an existing project owner",
            )

    token = mint_service_token()
    cred = ServiceCredential(
        id=credential_id,
        name=payload.name,
        description=payload.description,
        principal_user_id=principal_user_id,
        token_hash=hash_service_token(token),
        token_prefix=token[:12],
        scopes=payload.scopes,
        expires_at=_utcnow() + timedelta(days=payload.expires_in_days),
        created_by=current_user["user_id"],
    )
    try:
        async with session.begin_nested():
            session.add(cred)
            await session.flush()
    except IntegrityError:
        # A concurrent mint of the same name (or a full token_hash collision,
        # astronomically unlikely) won the unique constraint race.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Service credential name '{payload.name}' is already in use",
        ) from None

    for membership in payload.project_memberships:
        await _upsert_service_principal_membership(
            session, membership.project_id, principal_user_id, membership.role
        )

    await session.commit()
    await session.refresh(cred)

    response = _credential_public_dict(cred)
    response["token"] = token
    response["token_notice"] = "Store this token now; it is not retrievable again."
    return response


@router.get("/service-credentials")
async def list_service_credentials(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """List all service credentials. Never returns token/hash fields."""
    result = await session.execute(
        select(ServiceCredential).order_by(ServiceCredential.created_at.desc())
    )
    return {
        "service_credentials": [
            _credential_public_dict(cred) for cred in result.scalars().all()
        ]
    }


@router.post("/service-credentials/{credential_id}/revoke")
async def revoke_service_credential(
    credential_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Revoke a service credential immediately. Idempotent.

    Does NOT delete the principal's ProjectMember rows — those memberships
    belong to ``principal_user_id``, not to this credential row, so revoking
    one credential must not strip access a still-live sibling credential (or a
    future remint under the same principal) relies on.
    """
    cred = await session.get(ServiceCredential, credential_id)
    if cred is None:
        raise HTTPException(status_code=404, detail="Service credential not found")
    if cred.revoked_at is None:
        cred.revoked_at = _utcnow()
        session.add(cred)
        await session.commit()
        await session.refresh(cred)
    return _credential_public_dict(cred)
