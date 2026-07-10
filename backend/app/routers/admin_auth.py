"""Admin token-exchange endpoint: ``POST /api/admin/token``.

Provides a short-lived JWT exchange for the shared ADMIN_API_KEY, so
callers can migrate from the long-lived shared secret to short-lived
bearer tokens.  The endpoint is available only when ``_auth.ADMIN_JWT_SECRET``
is configured on the server.

Routes:
    POST /api/admin/token  — exchange ADMIN_API_KEY for a short-lived JWT
"""

import logging
import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app import auth as _auth
from app.database import get_session
from app.models import AllowedUser

router = APIRouter(prefix="/api/admin")
logger = logging.getLogger(__name__)


class TokenResponse(BaseModel):
    """Response payload for a successful token exchange."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


@router.post("/token", response_model=TokenResponse)
async def exchange_admin_api_key(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Exchange ``ADMIN_API_KEY`` for a short-lived admin JWT.

    The ``ADMIN_API_KEY`` must be presented in the ``Authorization`` header
    as a ``Bearer`` token.  The returned JWT is valid for
    ``ADMIN_JWT_EXPIRE_MINUTES`` (default 60 minutes) and can be used in
    place of the shared secret on all admin endpoints.

    Requires ``_auth.ADMIN_JWT_SECRET`` to be configured server-side; returns
    ``501 Not Implemented`` otherwise.
    """
    # --- Precondition: JWT signing must be configured -----------------------
    if not _auth.ADMIN_JWT_SECRET:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Admin JWT exchange not configured (_auth.ADMIN_JWT_SECRET not set)",
        )

    # --- Extract Bearer token ----------------------------------------------
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header — expected 'Bearer <ADMIN_API_KEY>'",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented_key = auth_header[len("Bearer "):]

    # --- Validate against ADMIN_API_KEY ------------------------------------
    admin_api_key = os.getenv("ADMIN_API_KEY")
    if not admin_api_key or not secrets.compare_digest(presented_key, admin_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # --- Source-IP allowlist check -----------------------------------------
    if not _auth._check_admin_api_key_source_ip(request):
        logger.warning(
            "ADMIN_API_KEY token exchange rejected: source IP not in allowlist"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin API key usage not allowed from this IP",
        )

    # --- Audit log ----------------------------------------------------------
    _auth._audit_log_admin_api_key(request, action="token_exchange")

    # --- Resolve the real admin UID (same logic as get_current_user) -------
    admin_uid = "admin-api-key"
    try:
        result = await session.execute(
            select(AllowedUser)
            .where(AllowedUser.role == "admin")
            .order_by(AllowedUser.created_at.asc())
        )
        admin_user = result.scalars().first()
        if admin_user and admin_user.firebase_uid:
            admin_uid = admin_user.firebase_uid
    except Exception:
        logger.exception(
            "Failed to resolve admin UID from DB; falling back to literal"
        )

    # --- Issue the JWT ------------------------------------------------------
    jwt_token = _auth.create_admin_token(user_id=admin_uid)

    return TokenResponse(
        access_token=jwt_token,
        token_type="bearer",
        expires_in=_auth._ADMIN_JWT_EXPIRE_MINUTES * 60,
    )
