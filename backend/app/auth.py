"""Authentication and authorization dependencies for the orchestrator API.

Covers the platform's authentication schemes: Firebase ID tokens for
admin/operator users (verified via the Firebase Admin SDK, initialized at
module load), an ``ADMIN_API_KEY`` shared-secret path for programmatic
access, and short-lived agent JWTs for VM-side requests. Also provides
helpers for minting and verifying agent JWTs and WebSocket admission tickets,
plus the ``get_current_user`` / ``require_admin`` FastAPI dependencies that
resolve the caller against the ``AllowedUser`` table.
"""

from fastapi import Security, HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
import os
import logging
import jwt
import secrets
from datetime import datetime, timedelta, timezone
import firebase_admin
from firebase_admin import auth as firebase_auth

from app.database import get_session
from app.models import AllowedUser, _utcnow

logger = logging.getLogger(__name__)

# Initialize Firebase Admin SDK
# In GCP Cloud Run, this automatically uses the default service account credentials.
# Locally, it uses the GOOGLE_APPLICATION_CREDENTIALS environment variable.
try:
    firebase_admin.initialize_app()
except Exception as e:  # noqa: BLE001 — module-load init must never crash the import
    # App already initialized, or Firebase not configured yet (placeholder secrets)
    logger.warning(f"Firebase Admin SDK init: {e}")

# We use HTTPBearer to automatically extract the "Authorization: Bearer <token>" header
security = HTTPBearer()

# Secret key for signing Agent JWTs
_agent_jwt_secret = os.getenv("AGENT_JWT_SECRET")
if not _agent_jwt_secret:
    raise ValueError("AGENT_JWT_SECRET environment variable is required")
AGENT_JWT_SECRET: str = _agent_jwt_secret
AGENT_JWT_ALGORITHM = "HS256"

# Secret key for signing WebSocket tickets
_ws_ticket_secret = os.getenv("WS_TICKET_SECRET")
if not _ws_ticket_secret:
    raise ValueError("WS_TICKET_SECRET environment variable is required")
WS_TICKET_SECRET: str = _ws_ticket_secret


def create_ws_ticket(user_id: str, project_id: str) -> str:
    """
    Generate a short-lived ticket for WebSocket authentication.
    """
    expire = datetime.now(timezone.utc) + timedelta(minutes=1)  # Valid for 1 minute
    to_encode = {
        "sub": user_id,
        "project_id": project_id,
        "exp": expire,
        "type": "ws_ticket",
    }
    return jwt.encode(to_encode, WS_TICKET_SECRET, algorithm=AGENT_JWT_ALGORITHM)


def verify_ws_ticket(ticket: str) -> dict:
    """
    Verify a WebSocket ticket.
    """
    try:
        payload = jwt.decode(ticket, WS_TICKET_SECRET, algorithms=[AGENT_JWT_ALGORITHM])
        if payload.get("type") != "ws_ticket":
            raise HTTPException(status_code=401, detail="Invalid ticket type")
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Ticket expired") from None
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid ticket") from None


# 1h is the maximum replay window for any path that lacks per-request
# revalidation (e.g. the WS receive loop). The agent MUST call
# /api/agent/refresh-token proactively (at ~45 min) before the token
# expires; tryRefreshToken() in index.ts handles this. Raising this
# value extends the post-termination replay window — do not increase
# without also shortening the proactive refresh interval.
AGENT_TOKEN_TTL_HOURS = 1

# Long-lived *bootstrap* window granted ONLY to cold-start agents that have
# not yet proven they can refresh. A non-atomic backend-deploy / agent-rebake
# cutover can straddle token expiry; an old-image agent that cannot call
# /api/agent/refresh-token would 401 mid-task if it held only the 1h token.
# The bootstrap window keeps such an in-flight agent alive across that cutover.
# This wider replay window is a deliberate, bounded cost paid ONLY until an
# agent demonstrates refresh capability: the moment it successfully calls
# /api/agent/refresh-token (or is warm-handed-off on a VM already proven
# refresh-capable) it is rotated back down to AGENT_TOKEN_TTL_HOURS. Do NOT
# use this TTL for refresh-confirmed agents.
AGENT_BOOTSTRAP_TOKEN_TTL_HOURS = 8


def create_agent_token(
    task_id: str,
    vm_instance_id: str,
    task_type: str = "",
    project_id: str = "",
    ttl_hours: int = AGENT_TOKEN_TTL_HOURS,
) -> str:
    """Generate a short-lived JWT for a specific agent VM.

    Audit 2026-04-25 (W3): claims include project_id (E10) so per-endpoint
    authz (E2/E3/E10) can validate without an extra DB lookup. TTL is 1h;
    the agent refreshes proactively at ~45 min via /api/agent/refresh-token
    so long-running tasks never hit a mid-execution 401.
    """
    expire = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    to_encode = {
        "sub": str(task_id),
        "vm_instance_id": vm_instance_id,
        "task_type": task_type,
        "project_id": project_id,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    encoded_jwt = jwt.encode(to_encode, AGENT_JWT_SECRET, algorithm=AGENT_JWT_ALGORITHM)
    return encoded_jwt


# Token-scope marker for VM-scoped (taskless) agent JWTs. A normal agent token
# is TASK-scoped: ``sub`` carries the task_id and per-endpoint authz validates
# the task binding. A warm-pool VM, however, boots *before* it has claimed any
# task, so it cannot hold a task-scoped token. A VM-scoped token carries
# ``vm_instance_id`` + ``project_id`` and ``token_scope == "vm"`` but NO task
# ``sub`` — the taskless endpoints (vm-heartbeat / next-role) accept it and
# scope every action by ``vm_instance_id`` instead of a task. (/heartbeat and
# /claim still require a task ``sub`` and reject a bare VM-scoped token; a pool
# VM only reaches those AFTER /next-role rotates it to a task-scoped token.)
VM_SCOPED_TOKEN_SCOPE = "vm"


def create_vm_scoped_token(
    vm_instance_id: str,
    project_id: str,
    ttl_hours: int = AGENT_BOOTSTRAP_TOKEN_TTL_HOURS,
) -> str:
    """Mint a VM-scoped (taskless) agent JWT for a warm-pool VM.

    Unlike ``create_agent_token`` this token has NO task ``sub`` — it identifies
    only the VM (``vm_instance_id``) and its tenant (``project_id``) and is
    tagged ``token_scope = "vm"``. It authenticates a pool VM that has not yet
    claimed a task on the taskless agent endpoints; once the VM claims/leases a
    task it is handed a fresh task-scoped token (e.g. via the warm-claim
    finalizer), exactly like a cold VM.

    Defaults to the long bootstrap TTL because, like a cold-start VM, a freshly
    booted pool VM has not yet proven it can refresh; the dispatch path rotates
    it down to the short TTL once it claims work.
    """
    expire = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    to_encode = {
        # No task ``sub``: this token is not bound to any task.
        "token_scope": VM_SCOPED_TOKEN_SCOPE,
        "vm_instance_id": vm_instance_id,
        "project_id": project_id,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(to_encode, AGENT_JWT_SECRET, algorithm=AGENT_JWT_ALGORITHM)


def verify_agent_jwt(token: str) -> dict:
    """
    Verify an agent's JWT.
    """
    try:
        payload = jwt.decode(token, AGENT_JWT_SECRET, algorithms=[AGENT_JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Agent token expired") from None
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid agent token") from None


# How stale ``last_login_at`` may get before we refresh it. The timestamp is
# cosmetic (operator visibility), so a coarse cadence is fine — and it keeps the
# auth dependency from writing+committing on EVERY authenticated request. See
# _persist_auth_metadata_best_effort.
_LAST_LOGIN_UPDATE_INTERVAL = timedelta(minutes=15)

# Short, per-statement lock timeout for the best-effort auth-metadata write so it
# fails fast instead of blocking the auth hot path on a contended row (the global
# default is 30s). On timeout we degrade — skip the write — rather than 500.
_AUTH_METADATA_LOCK_TIMEOUT = "1s"


async def _persist_auth_metadata_best_effort(
    session: AsyncSession, allowed_user: AllowedUser
) -> None:
    """Commit non-critical AllowedUser session metadata, degrading on contention.

    ``last_login_at`` / ``display_name`` / first-sign-in ``firebase_uid`` linking
    are all non-critical. Previously they were written and committed on EVERY
    authenticated request, which made ``get_current_user`` a write on the auth
    hot path: when the DB was contended (a lock-wedged ``task`` row + connection
    pool starvation, incident 2026-06-04) this commit timed out and 500'd EVERY
    authenticated request, taking the whole admin UI down.

    Now the caller only invokes this when something actually changed, and the
    write runs behind a short ``lock_timeout`` and swallows DB errors: a contended
    row simply skips the update (it self-heals on the next request) instead of
    failing authentication. The caller MUST snapshot anything it needs off
    ``allowed_user`` BEFORE calling this — a rollback here expires the instance,
    and a later attribute read would lazy-load under async (MissingGreenlet).
    """
    try:
        # Fail fast rather than parking a pooled connection on a contended row.
        await session.execute(
            text("SET LOCAL lock_timeout = :t").bindparams(
                t=_AUTH_METADATA_LOCK_TIMEOUT
            )
        )
        session.add(allowed_user)
        await session.commit()
    except SQLAlchemyError:
        logger.warning(
            "[AUTH] best-effort AllowedUser metadata persist skipped "
            "(DB contention / transient error); authentication proceeds",
            exc_info=True,
        )
        try:
            await session.rollback()
        except SQLAlchemyError:
            logger.exception(
                "[AUTH] rollback after failed metadata persist also failed"
            )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(security),
    session: AsyncSession = Depends(get_session),
):
    """
    FastAPI Dependency to verify the frontend user's JWT using Firebase Auth,
    then look up the user in the AllowedUser table for role assignment.
    Also allows programmatic access via ADMIN_API_KEY.
    """
    token = credentials.credentials

    admin_api_key = os.getenv("ADMIN_API_KEY")
    if admin_api_key and secrets.compare_digest(token, admin_api_key):
        # Resolve real admin UID so API key has same data access as the admin user
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
        return {"user_id": admin_uid, "email": "admin@system.local", "role": "admin"}

    try:
        decoded_token = firebase_auth.verify_id_token(token)
    except (
        firebase_admin.auth.InvalidIdTokenError,
        firebase_admin.auth.ExpiredIdTokenError,
        firebase_admin.auth.RevokedIdTokenError,
        firebase_admin.auth.UserDisabledError,
        firebase_admin.auth.CertificateFetchError,
        ValueError,
        KeyError,
    ) as e:
        logger.warning(f"Auth token validation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e
    except Exception as e:
        logger.error(f"Unexpected auth error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication service error",
        ) from e

    uid = decoded_token.get("uid")
    email = decoded_token.get("email")

    if not decoded_token.get("email_verified", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email address must be verified.",
        )

    # Look up user in AllowedUser table
    result = await session.execute(
        select(AllowedUser).where(AllowedUser.email == email)
    )
    allowed_user = result.scalar_one_or_none()

    if not allowed_user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized. Contact an administrator to request access.",
        )

    # Maintain non-critical session metadata, but only commit when something
    # actually changed and only as a best effort — this runs on EVERY
    # authenticated request, so an unconditional write+commit here turns any DB
    # contention into a total auth/UI outage (incident 2026-06-04). The common
    # request changes nothing and skips the write entirely.
    needs_persist = False
    # Link Firebase UID on first sign-in
    if not allowed_user.firebase_uid:
        allowed_user.firebase_uid = uid
        needs_persist = True
    # Update display name from Firebase if available
    display_name = decoded_token.get("name")
    if display_name and allowed_user.display_name != display_name:
        allowed_user.display_name = display_name
        needs_persist = True
    # Track last login, throttled — minute-granularity is irrelevant for this
    # cosmetic timestamp, and throttling keeps the hot path write-free.
    now = _utcnow()
    if (
        allowed_user.last_login_at is None
        or (now - allowed_user.last_login_at) >= _LAST_LOGIN_UPDATE_INTERVAL
    ):
        allowed_user.last_login_at = now
        needs_persist = True

    # Snapshot the role BEFORE any commit/rollback: get_current_user must still
    # return it even if the best-effort persist below rolls back (a rollback
    # expires the instance, and a later read would lazy-load -> MissingGreenlet).
    role = allowed_user.role

    if needs_persist:
        await _persist_auth_metadata_best_effort(session, allowed_user)

    return {
        "user_id": uid,
        "email": email,
        "email_verified": decoded_token.get("email_verified", False),
        "role": role,
    }


def require_admin(current_user: dict = Depends(get_current_user)):
    """
    Dependency to ensure the current user has admin privileges.
    """
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required"
        )
    return current_user
