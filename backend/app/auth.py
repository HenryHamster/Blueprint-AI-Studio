"""Authentication and authorization dependencies for the orchestrator API.

Covers the platform's authentication schemes: Firebase ID tokens for
admin/operator users (verified via the Firebase Admin SDK, initialized at
module load), an ``ADMIN_API_KEY`` shared-secret path for programmatic
access, and short-lived agent JWTs for VM-side requests. Also provides
helpers for minting and verifying agent JWTs and WebSocket admission tickets,
plus the ``get_current_user`` / ``require_admin`` FastAPI dependencies that
resolve the caller against the ``AllowedUser`` table.
"""

from fastapi import Security, HTTPException, status, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
import os
import logging
import jwt
import secrets
import hashlib
import uuid
import ipaddress as _ipaddress
from datetime import datetime, timedelta, timezone
import firebase_admin
from firebase_admin import auth as firebase_auth

from app.database import get_session, resolve_dialect_name
from app.models import AllowedUser, LocalRunner, _utcnow

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
# Runner endpoints promise a 401 for both missing and invalid bearer tokens.
# ``HTTPBearer`` otherwise rejects a missing header with 403 before the runner
# dependency can apply that contract.
runner_security = HTTPBearer(auto_error=False)


def _validate_jwt_secret(secret: str, env_var: str) -> None:
    secret_bytes = secret.encode()
    if len(secret_bytes) < 32:
        raise ValueError(
            f"{env_var} must be at least 32 bytes — "
            f"got {len(secret_bytes)} bytes. JWT secret must be at least 32 bytes"
        )


# Secret key for signing Agent JWTs
_agent_jwt_secret = os.getenv("AGENT_JWT_SECRET")
if not _agent_jwt_secret:
    raise ValueError("AGENT_JWT_SECRET environment variable is required")
_validate_jwt_secret(_agent_jwt_secret, "AGENT_JWT_SECRET")
AGENT_JWT_SECRET: str = _agent_jwt_secret
AGENT_JWT_ALGORITHM = "HS256"

# Secret key for signing WebSocket tickets
_ws_ticket_secret = os.getenv("WS_TICKET_SECRET")
if not _ws_ticket_secret:
    raise ValueError("WS_TICKET_SECRET environment variable is required")
_validate_jwt_secret(_ws_ticket_secret, "WS_TICKET_SECRET")
WS_TICKET_SECRET: str = _ws_ticket_secret

# ---------------------------------------------------------------------------
# Admin JWT (short-lived token exchange for ADMIN_API_KEY)
# ---------------------------------------------------------------------------
#
# ADMIN_JWT_SECRET is OPTIONAL. When set, callers can exchange ADMIN_API_KEY
# for a short-lived JWT via POST /api/admin/token, and all admin endpoints
# accept that JWT. When unset, the legacy direct ADMIN_API_KEY check remains
# the only path (with audit logging and optional IP allowlist enforcement).

_CRANKED_admin_jwt_secret = os.getenv("ADMIN_JWT_SECRET")
ADMIN_JWT_SECRET: str | None = (
    _validate_jwt_secret(_CRANKED_admin_jwt_secret, "ADMIN_JWT_SECRET")
    or _CRANKED_admin_jwt_secret
) if _CRANKED_admin_jwt_secret else None

ADMIN_JWT_ALGORITHM = "HS256"

# Admin JWT expiry (default 1 hour, configurable via env)
_ADMIN_JWT_EXPIRE_MINUTES = int(os.getenv("ADMIN_JWT_EXPIRE_MINUTES", "60"))


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

# Scope marker for tool-asset capability JWTs (minted by the tool-asset serve
# path in ``app.routers.admin_project_tools``). These tokens carry
# ``scope == "tool-asset"`` and are ONLY valid on the tool-asset download path —
# they must never authenticate to unrelated agent read surfaces (metrics reader,
# efficiency-snapshot read). ``reject_tool_asset_scope`` enforces that boundary.
TOOL_ASSET_TOKEN_SCOPE = "tool-asset"


def reject_tool_asset_scope(payload: dict) -> None:
    """Reject a tool-asset-scoped JWT on general agent read surfaces.

    Tool-asset capability tokens are signed with the same ``AGENT_JWT_SECRET`` as
    agent tokens but are minted solely to serve static tool assets. They must not
    be usable to authenticate to unrelated read surfaces, so any decoded payload
    carrying ``scope == "tool-asset"`` is rejected with 403 (out of scope), not
    401 (which callers may treat as "try the next auth method").
    """
    if payload.get("scope") == TOOL_ASSET_TOKEN_SCOPE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tool-asset token is out of scope for this surface",
        )


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


# ---------------------------------------------------------------------------
# Admin JWT — short-lived token exchange
# ---------------------------------------------------------------------------


def _parse_admin_ip_allowlist() -> list:
    """Parse ADMIN_API_KEY_ALLOWED_IPS into a list of ip_network objects.

    Returns an empty list when the env var is unset or empty (meaning no
    IP restriction — the default). Each entry is a CIDR notation string;
    entries are separated by commas.
    """
    raw = os.getenv("ADMIN_API_KEY_ALLOWED_IPS", "")
    if not raw:
        return []
    nets: list = []
    for entry in raw.split(","):
        entry = entry.strip()
        if entry:
            try:
                nets.append(_ipaddress.ip_network(entry, strict=False))
            except ValueError:
                logger.warning("Ignoring invalid CIDR in ADMIN_API_KEY_ALLOWED_IPS: %r", entry)
    return nets


def _check_admin_api_key_source_ip(request: Request | None) -> bool:
    """Check *request* source-IP against ADMIN_API_KEY_ALLOWED_IPS.

    Returns ``True`` when either:
    * No allowlist is configured (env var is empty/unset).
    * The request client IP falls within at least one allowlist entry.

    Returns ``False`` (denying access) when:
    * An allowlist IS configured and the request IP does not match.
    * ``request`` is ``None`` or has no ``client`` (defensive — caller should
      always pass a real request in production).
    """
    allowlist = _parse_admin_ip_allowlist()
    if not allowlist:
        return True  # No allowlist configured = no restriction
    if request is None or request.client is None:
        return False  # Missing request context = deny closed
    try:
        src = _ipaddress.ip_address(request.client.host)
        return any(src in net for net in allowlist)
    except ValueError:
        return False


def _audit_log_admin_api_key(request: Request | None, *, action: str) -> None:
    """Emit a structured log entry recording that ADMIN_API_KEY was used.

    *action* describes the usage context — ``"direct_use"`` (legacy bearer
    check), ``"token_exchange"`` (JWT exchange endpoint), etc.
    """
    extra: dict = {
        "audit_event": "admin_api_key_usage",
        "action": action,
    }
    if request is not None:
        extra["source_ip"] = (
            request.client.host if request.client else "unknown"
        )
        extra["user_agent"] = request.headers.get("user-agent", "")
    else:
        extra["source_ip"] = "unknown"
        extra["user_agent"] = ""
    logger.warning("ADMIN_API_KEY used [action=%s]", action, extra=extra)


def create_admin_token(
    user_id: str,
    expire_minutes: int | None = None,
) -> str:
    """Mint a short-lived JWT that authenticates as ``user_id`` with ``role=admin``.

    Requires ``ADMIN_JWT_SECRET`` to be set (raises ``ValueError`` otherwise).
    The token carries ``token_type=admin_api_exchange`` so token-type checks
    in ``verify_admin_token`` prevent non-admin JWTs from being accepted.
    """
    if not ADMIN_JWT_SECRET:
        raise ValueError("ADMIN_JWT_SECRET is not configured")
    if expire_minutes is None:
        expire_minutes = _ADMIN_JWT_EXPIRE_MINUTES
    expire = datetime.now(timezone.utc) + timedelta(minutes=expire_minutes)
    to_encode: dict = {
        "sub": user_id,
        "role": "admin",
        "token_type": "admin_api_exchange",
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(to_encode, ADMIN_JWT_SECRET, algorithm=ADMIN_JWT_ALGORITHM)


def verify_admin_token(token: str) -> dict | None:
    """Verify an admin JWT signed with ``ADMIN_JWT_SECRET``.

    Returns the decoded payload dict on success, or ``None`` when:
    * ``ADMIN_JWT_SECRET`` is not configured.
    * The token has expired, is malformed, or has wrong ``token_type``/``role``.
    """
    if not ADMIN_JWT_SECRET:
        return None
    try:
        payload: dict = jwt.decode(
            token, ADMIN_JWT_SECRET, algorithms=[ADMIN_JWT_ALGORITHM]
        )
        if payload.get("token_type") != "admin_api_exchange":
            return None
        if payload.get("role") != "admin":
            return None
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.PyJWTError:
        return None


# ---------------------------------------------------------------------------
# Pull-based fleet runner authentication
# ---------------------------------------------------------------------------
#
# A LocalRunner enrolls on operator hardware and authenticates its polling
# (/api/runner/*) calls with a long-lived bearer secret. Only the SHA256 hash
# of that secret is ever persisted (LocalRunner.token_hash) — the plaintext is
# shown exactly once at registration and never stored.


def hash_runner_token(token: str) -> str:
    """Return the SHA256 hex digest used to store/look up a runner token."""
    return hashlib.sha256(token.encode()).hexdigest()


def create_runner_token(runner_id: uuid.UUID) -> str:
    """Mint a cryptographically secure secret token for a fleet runner.

    The CALLER is responsible for storing only the hash
    (``hash_runner_token(token)``) on the LocalRunner row; this function returns
    the plaintext token, which is surfaced to the operator exactly once.
    ``runner_id`` is accepted for call-site clarity/auditing; the token itself
    is an opaque random secret (no runner identity is encoded in it).
    """
    return secrets.token_urlsafe(32)


async def verify_runner_token(
    token: str, session: AsyncSession
) -> "LocalRunner | None":
    """Resolve a non-deleted LocalRunner by its bearer token, or None.

    Looks up the row whose ``token_hash`` matches SHA256(token) and whose
    ``status`` is anything other than ``"deleted"``.  An ``inactive`` runner
    (one the stale-runner sweep flipped off after a single missed heartbeat
    window — e.g. a slept or network-dropped laptop) MUST still authenticate so
    its heartbeat can re-arm it (set status back to ``"active"``); resolving only
    ``"active"`` rows permanently locked such runners out.  Deleted runners and
    unknown tokens never authenticate.

    Dispatch safety is unaffected: pinning targets runners filtered on a recent
    ``last_seen_at`` (orchestrator side), so authenticating an inactive runner
    here does not make it eligible for work until it heartbeats again.
    """
    token_hash = hash_runner_token(token)
    result = await session.execute(
        select(LocalRunner).where(
            LocalRunner.token_hash == token_hash,
            LocalRunner.status != "deleted",
        )
    )
    return result.scalars().first()


async def get_current_runner(
    credentials: HTTPAuthorizationCredentials | None = Security(runner_security),
    session: AsyncSession = Depends(get_session),
) -> LocalRunner:
    """FastAPI dependency authenticating a pull-based fleet runner.

    Raises 401 when the bearer token does not resolve to a non-deleted runner.
    """
    runner = (
        await verify_runner_token(credentials.credentials, session)
        if credentials is not None
        else None
    )
    if runner is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid runner token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return runner


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
        # SET LOCAL is Postgres-only and asyncpg CANNOT bind a parameter into it
        # (it is not a preparable statement, so a bound ``:t`` becomes a literal
        # ``$1`` → "syntax error at or near $1" on EVERY authenticated request).
        # Gate on the dialect and inline the trusted module constant as a literal.
        # SQLite/other test dialects have no lock_timeout GUC, so skipping it
        # there also keeps the best-effort metadata write working under tests.
        if resolve_dialect_name(session) == "postgresql":
            await session.execute(
                text(f"SET LOCAL lock_timeout = '{_AUTH_METADATA_LOCK_TIMEOUT}'")
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
    request: Request = None,
):
    """
    FastAPI Dependency to verify the frontend user's JWT using Firebase Auth,
    then look up the user in the AllowedUser table for role assignment.
    Also allows programmatic access via ADMIN_API_KEY or an admin JWT
    (short-lived token obtained from ``POST /api/admin/token``).
    """
    token = credentials.credentials

    # 1. Try admin JWT first (short-lived token from /api/admin/token exchange).
    #    This is the preferred programmatic path when ADMIN_JWT_SECRET is set.
    admin_jwt_payload = verify_admin_token(token)
    if admin_jwt_payload is not None:
        return {
            "user_id": admin_jwt_payload["sub"],
            "email": "admin@system.local",
            "role": "admin",
        }

    # 2. Fall back to ADMIN_API_KEY shared-secret (legacy path).
    admin_api_key = os.getenv("ADMIN_API_KEY")
    if admin_api_key and secrets.compare_digest(token, admin_api_key):
        # Source-IP allowlist enforcement (opt-in).
        if not _check_admin_api_key_source_ip(request):
            logger.warning(
                "ADMIN_API_KEY rejected from disallowed IP",
                extra={
                    "audit_event": "admin_api_key_rejected",
                    "source_ip": request.client.host if request and request.client else "unknown",
                },
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin API key usage not allowed from this IP",
            )

        # Audit log every direct ADMIN_API_KEY authentication.
        _audit_log_admin_api_key(request, action="direct_use")

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


async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Security(
        HTTPBearer(auto_error=False)
    ),
    session: AsyncSession = Depends(get_session),
) -> dict | None:
    """Like get_current_user but returns None instead of raising when no credentials.

    Used by endpoints that accept EITHER admin Bearer auth OR an agent JWT via
    a separate header (x-agent-token).  When credentials are absent the handler
    checks the agent-token header; when credentials are present the normal
    get_current_user path runs.
    """
    if credentials is None:
        return None
    return await get_current_user(credentials=credentials, session=session)


def require_admin(current_user: dict = Depends(get_current_user)):
    """
    Dependency to ensure the current user has admin privileges.
    """
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required"
        )
    return current_user


# METRICS_READER_ENDPOINTS: grep this token to audit which endpoints accept agent JWTs.
# This dependency MUST be used ONLY on read-only, non-mutating endpoints that expose
# non-tenant-sensitive data (e.g. per-instance latency percentiles, tier counts).
# NEVER use it on mutating endpoints or endpoints returning project/tenant data.
async def get_metrics_reader(
    credentials: HTTPAuthorizationCredentials = Security(security),
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> dict:
    """FastAPI dependency for READ-ONLY operator-metric endpoints.

    Accepts EITHER:
    - A short-lived admin JWT (from ``POST /api/admin/token``)
    - A token matching ADMIN_API_KEY (existing server-to-server admin path)
    - A valid agent JWT signed with AGENT_JWT_SECRET (task-scoped or VM-scoped)
    - A Firebase token for an allowed admin user (browser admin UI path)

    Returns a small principal dict for logging/audit. Does NOT call into Firebase
    unless the token is neither ADMIN_API_KEY nor an agent JWT. This is a
    deliberately narrow path — READ-ONLY metric access only. It MUST NEVER be
    placed on mutating endpoints.
    """
    token = credentials.credentials

    # 1. Admin JWT (short-lived token exchange — preferred programmatic path).
    admin_jwt_payload = verify_admin_token(token)
    if admin_jwt_payload is not None:
        return {"role": "admin"}

    # 2. ADMIN_API_KEY legacy path with audit logging + IP allowlist.
    admin_api_key = os.getenv("ADMIN_API_KEY")
    if admin_api_key and secrets.compare_digest(token, admin_api_key):
        if not _check_admin_api_key_source_ip(request):
            logger.warning("ADMIN_API_KEY rejected from disallowed IP in metrics_reader")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin API key usage not allowed from this IP",
            )
        _audit_log_admin_api_key(request, action="metrics_reader")
        return {"role": "admin"}

    try:
        # verify_agent_jwt raises HTTPException(401) on expired or invalid tokens.
        payload = verify_agent_jwt(token)
        # A tool-asset capability token must not authenticate to the metrics
        # reader — reject it with 403 (out of scope) rather than treating it as a
        # valid agent principal. The 403 propagates through the handler below
        # (which only swallows 401 to fall through to the Firebase path).
        reject_tool_asset_scope(payload)
        return {
            "role": "metrics_reader",
            "sub": payload.get("sub"),
            "vm_instance_id": payload.get("vm_instance_id"),
        }
    except HTTPException as agent_auth_error:
        if agent_auth_error.status_code != status.HTTP_401_UNAUTHORIZED:
            raise

    current_user = await get_current_user(credentials=credentials, session=session)
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required"
        )
    return current_user
