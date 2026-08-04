"""General service-credential (machine token) authentication seam.

A ``ServiceCredential`` is a hashed bearer token that authenticates as a
configurable non-human ``principal_user_id`` and is accepted ONLY on a
per-scope allowlist of (method, path) endpoints — see ``SCOPE_DEFINITIONS``
below. The first consumer is the Hermes read-only observer, but the
primitive is generic: new scopes are registered here as new consumers
appear. A service credential must never satisfy ``require_admin`` and must
never reach an endpoint outside its scopes.

``app.auth`` imports this module (to wire the token branch into
``get_current_user``), so this module must NOT import from ``app.auth`` —
doing so would form an import cycle. ``hash_service_token`` therefore
duplicates ``app.auth.hash_runner_token``'s sha256-hexdigest logic locally
rather than importing it.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import timedelta

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import ServiceCredential
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# Prefixed so callers (and app.auth.get_current_user) can route a service
# token straight to this module without ever attempting Firebase verification
# on it — a bpsc_ token can never be a Firebase JWT.
TOKEN_PREFIX = "bpsc_"

# Best-effort last-used bookkeeping is throttled to avoid a write on every
# single authenticated request from a hot machine consumer (e.g. an observer
# polling loop) — mirrors the throttle pattern in app.auth's login metadata.
_LAST_USED_UPDATE_INTERVAL = timedelta(seconds=60)


def mint_service_token() -> str:
    """Mint a new plaintext service credential token.

    The caller is responsible for storing only ``hash_service_token(token)``;
    the plaintext is surfaced to the operator exactly once at mint time.
    """
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_service_token(token: str) -> str:
    """Return the SHA256 hex digest used to store/look up a service token."""
    return hashlib.sha256(token.encode()).hexdigest()


# Scope registry: each scope name maps to a list of (HTTP method, compiled
# path-regex) pairs allowlisting exactly which endpoints a credential holding
# that scope may call. Only membership-scoped endpoints (those that resolve
# access via ``_get_user_project``/``_get_user_project_with_role``, i.e. scope
# to the caller's ProjectMember rows) belong here — a service credential's
# reach is bounded by the ProjectMember rows minted for its principal, so the
# endpoint itself must also honor that boundary. Admin-only/global endpoints
# must never appear in a scope.
SCOPE_DEFINITIONS: dict[str, list[tuple[str, re.Pattern]]] = {
    # Read-only observation of authorized projects (Hermes observer).
    "observer:read": [
        # GET /api/admin/projects (admin_projects.py) — lists the CALLER's own
        # projects via a ProjectMember join filtered on current_user["user_id"];
        # membership-scoped, not admin-only. Verified against the live route.
        ("GET", re.compile(r"^/api/admin/projects/?$")),
        ("GET", re.compile(r"^/api/admin/operator-attention/?$")),
        ("GET", re.compile(r"^/api/admin/projects/[0-9a-fA-F-]{36}/diagnostics/?$")),
        ("GET", re.compile(r"^/api/admin/projects/[0-9a-fA-F-]{36}/resource-summary/?$")),
    ],
}


def request_allowed(scopes: list[str], method: str, path: str) -> bool:
    """Return whether *method*/*path* is permitted by the union of *scopes*.

    Unknown scope names (e.g. a credential minted under a scope later retired
    from the registry) are ignored rather than treated as an error — they
    simply grant nothing, so the credential fails closed instead of 500ing.
    """
    for scope in scopes:
        rules = SCOPE_DEFINITIONS.get(scope)
        if rules is None:
            logger.warning("Unknown service-credential scope %r ignored", scope)
            continue
        for allowed_method, pattern in rules:
            if method == allowed_method and pattern.match(path):
                return True
    return False


async def authenticate_service_token(
    token: str, session: AsyncSession, request: Request | None
) -> dict | None:
    """Resolve a ``bpsc_``-prefixed bearer token to a principal dict, or raise.

    Only called when ``token.startswith(TOKEN_PREFIX)`` — the caller
    (``app.auth.get_current_user``) routes on that prefix precisely so an
    invalid/unknown service token never falls through to Firebase
    verification (a bpsc_ token can never be a valid Firebase JWT).
    """
    result = await session.execute(
        select(ServiceCredential).where(
            ServiceCredential.token_hash == hash_service_token(token)
        )
    )
    cred = result.scalars().first()
    if cred is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid service credential",
        )
    if cred.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Service credential revoked",
        )
    if cred.expires_at is not None and cred.expires_at <= _utcnow():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Service credential expired",
        )

    # request is None only for direct/internal calls (no HTTP request to scope
    # against) — fail closed rather than assume unscoped access.
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Endpoint not permitted for this service credential",
        )
    if not request_allowed(cred.scopes, request.method, request.url.path):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Endpoint not permitted for this service credential",
        )

    # Rollback expires ORM instances. Snapshot the identity fields before the
    # best-effort telemetry write so a failed commit cannot break auth itself.
    principal_user_id = cred.principal_user_id
    credential_name = cred.name
    now = _utcnow()
    if cred.last_used_at is None or (now - cred.last_used_at) >= _LAST_USED_UPDATE_INTERVAL:
        try:
            cred.last_used_at = now
            cred.last_used_path = request.url.path[:255]
            session.add(cred)
            await session.commit()
        except Exception:
            # This bookkeeping must never fail the authenticated request itself.
            logger.exception("Failed to update service-credential last-used telemetry")
            await session.rollback()

    # Role is ALWAYS "user" — a service credential must never satisfy
    # require_admin, regardless of what scopes it was minted with.
    return {
        "user_id": principal_user_id,
        "email": f"{credential_name}@service.blueprint.local",
        "role": "user",
        "service_credential": credential_name,
    }
