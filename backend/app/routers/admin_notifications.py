"""Alert, message, and playbook management endpoints."""

import logging
import re
import uuid
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, update, or_, and_
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import get_session
from app.models import (
    Alert,
    Project,
    ProjectMember,
    ProjectMessage,
    MessageType,
    MESSAGE_CONTENT_MAX,
    MessagesLastSeen,
    MessagesNoisePrefs,
    MessagesSavedViews,
    _utcnow,
)
from app.auth import get_current_user
from app.shared_state import limiter
from app.admin import _get_user_project, _get_user_project_with_role, _user_project_ids
from app.task_locking import lock_project_or_429
from app import notification_dispatch as _nd
from app.playbook_storage import (
    _archive_least_useful_entry,
    _playbook_size_bytes,
    _PLAYBOOK_CAPACITY_BYTES,
    record_playbook_tombstone,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-notifications"],
    dependencies=[Depends(get_current_user)],
)


# ── Pydantic Models ───────────────────────────────────────────────────────────


class MessageCreate(BaseModel):
    project_id: str
    message_type: str = "NOTE"
    content: str
    context: Optional[Dict[str, Any]] = None


class MessageResolve(BaseModel):
    resolution: Optional[str] = None


class LastSeenUpdate(BaseModel):
    project_id: str
    timestamp: int  # Unix ms epoch


class NoisePrefUpdate(BaseModel):
    project_id: str
    prefs: Dict[str, Any] = Field(default_factory=dict)


class SavedViewsUpdate(BaseModel):
    project_id: str
    saved_views: List[Dict[str, Any]] = Field(default_factory=list)


class TempMutesUpdate(BaseModel):
    project_id: str
    temp_mutes: Dict[str, int] = Field(
        default_factory=dict
    )  # event_type -> expiry ms epoch


class MuteWindowUpdate(BaseModel):
    project_id: str
    expires_at: Optional[int] = None  # ms epoch; None clears the mute window


class AdminPlaybookSet(BaseModel):
    key: str = Field(
        ..., description="Playbook key: alphanumeric + hyphens, max 50 chars"
    )
    value: str = Field(..., description="Playbook value: plain text, max 2048 bytes")
    applicable_types: Optional[List[str]] = Field(
        None, description="Task types this entry applies to; None = shown to all types"
    )


# ── Constants ─────────────────────────────────────────────────────────────────

_ADMIN_PLAYBOOK_KEY_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]{0,49}$")

# Mirrors ROUTINE_EVENT_LABELS keys in admin-ui/src/components/MessagesPanel.utils.ts.
# Events in this set trigger a one-line digest appended to project.running_update.
ROUTINE_EVENT_TYPES: frozenset = frozenset(
    {
        "pr_merged",
        "direction_milestone",
        "cycle_summary",
        "account_model_regression_quarantined",
        "account_model_regression_recovered",
        "auto_fix_spawned",
        "auto_fix_result",
        "residual_fix_spawned",
        "residual_fix_result",
        "residual_fix_unsuspended",
        "residual_case_auto_resolved",
        "issue_implementation_spawned",
        "review_feedback",
        "feedback_acknowledgment",
    }
)


# System-emitted NOTE event types classified as high_signal in event_type_snapshot.json.
# These must always survive paging regardless of routine message volume, identical to
# how BLOCKER/FEEDBACK/BUG_REPORT are guaranteed.  This includes the operator steering
# receipt/confirmation pair so the queued receipt remains visible while it waits behind
# an active cycle. Escalation types (auto_fix_escalated, residual_fix_escalated, etc.)
# are already safe because they emit as BLOCKER messages.
HIGH_SIGNAL_NOTE_EVENT_TYPES: frozenset = frozenset(
    {
        "failure_alert",
        "rollback_recommended",
        "fix_loop_divergence_regressed",
        "direction_outcome_stalled",
        "promotion_readiness_reached",
        "release_calcifying",
        "release_build_stalled",
        "release_stabilize_stalled",
        "release_ci_unresolved_stall",
        "release_auto_merge_stalled",
        "strategic_steering_deferred",
        "strategic_steering_acted",
    }
)


# Reserved keys within MessagesNoisePrefs.prefs for temp-mutes and mute-window.
# Prefixed with underscore to avoid collision with event-type visibility keys.
_TEMP_MUTES_KEY = "_temp_mutes"
_MUTE_WINDOW_KEY = "_mute_window"
# These keys are owned by their own dedicated PUT endpoints; the noise-prefs
# (event-visibility) endpoint must never write them, only preserve them.
_RESERVED_PREFS_KEYS = (_TEMP_MUTES_KEY, _MUTE_WINDOW_KEY)


def append_routine_digest(project: Project, event_type: str, summary: str) -> None:
    """Append a one-line digest entry for a routine event to project.running_update.

    Thin wrapper delegating to the canonical ``Project.append_routine_digest``
    so the digest format lives in exactly one place. Does NOT commit — the
    caller owns the transaction (and, on the user-facing message path, the
    project row lock that makes the read-append-write concurrency-safe).
    """
    project.append_routine_digest(event_type, summary)


from app.payload_schemas import ALL_TASK_TYPES as _VALID_TASK_TYPES  # noqa: E402

_SEED_ENTRIES: Dict[str, dict] = {
    "build-backend": {
        "content": (
            "Run backend tests with: `bash scripts/test-backend.sh` (builds Docker, runs full suite). "
            "Specific file: `bash scripts/test-backend.sh testing/test_file.py`. "
            "Standalone (no DB): `bash scripts/test-backend.sh --standalone`. "
            "Do NOT use pytest directly."
        ),
        "applicable_types": [
            "create",
            "fix",
            "refactor",
            "qa",
            "migrate",
            "deps",
            "smoketest",
        ],
    },
    "build-frontend": {
        "content": (
            "Frontend validation: `cd admin-ui && npm run build && npx eslint .`. "
            "Both build and lint must pass."
        ),
        "applicable_types": ["create", "fix", "refactor", "qa", "deps"],
    },
    "build-agent": {
        "content": (
            "Agent type checking: `cd agent && npx tsc --noEmit`. "
            "No runtime tests — type checking only."
        ),
        "applicable_types": ["create", "fix", "refactor"],
    },
    "file-restrictions": {
        "content": (
            "NEVER modify: .github/workflows/ (CI managed separately, GitHub App token can't push), "
            "backend/alembic/ migrations (generated, not hand-edited), "
            "scripts/ (unless explicitly asked)."
        ),
        "applicable_types": None,
    },
    "main-py-policy": {
        "content": (
            "main.py is being decomposed. Do NOT add new functions. "
            "Use extracted modules: task_completion.py (completion handlers), "
            "task_lifecycle.py (pipeline/lifecycle), max_plan_pool.py (account pool). "
            "A regression guard in test_invariants.py enforces the function count cap."
        ),
        "applicable_types": ["create", "fix", "refactor"],
    },
    "task-accessor": {
        "content": (
            "Use task.first_payload for the first payload element. "
            "Never use (task.payloads or [{}])[0]."
        ),
        "applicable_types": ["create", "fix", "refactor", "review"],
    },
    "admin-auth": {
        "content": (
            "New admin endpoints require @require_project_access or @require_role decorators. "
            "Follow the pattern in admin.py using _get_user_project_with_role()."
        ),
        "applicable_types": ["create", "fix"],
    },
    "import-from-source": {
        "content": (
            "Import from extracted modules directly, not via main.py re-exports. "
            "task_completion.py has completion handlers, task_lifecycle.py has pipeline/review/merge logic, "
            "max_plan_pool.py has account pooling."
        ),
        "applicable_types": ["create", "fix", "refactor"],
    },
    "test-diagnosis": {
        "content": (
            "When fixing test failures, FIRST diagnose: is the failure in the CODE or the TEST? "
            "Don't delete or skip tests to make them pass. "
            "Only remove tests when the tested functionality itself was removed."
        ),
        "applicable_types": ["fix", "qa", "debug"],
    },
    "circular-dep-warning": {
        "content": (
            "main.py and orchestrator.py have a circular dependency. "
            "Orchestrator imports completion handlers from main, main imports orchestrator state. "
            "Use extracted modules to avoid deepening this cycle."
        ),
        "applicable_types": ["create", "fix", "refactor"],
    },
}


# ── Alert Endpoints ───────────────────────────────────────────────────────────


@router.get("/alerts")
async def get_alerts(
    project_id: Optional[str] = None,
    include_dismissed: bool = False,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    query = (
        select(Alert)
        .where(Alert.project_id.in_(_user_project_ids(current_user["user_id"])))
        .order_by(Alert.created_at.desc())
    )
    if project_id:
        query = query.where(Alert.project_id == uuid.UUID(project_id))
    if not include_dismissed:
        query = query.where(Alert.dismissed_at.is_(None))
    query = query.limit(limit).offset(offset)
    result = await session.execute(query)
    alerts = result.scalars().all()
    return [
        {
            "id": str(a.id),
            "project_id": str(a.project_id),
            "severity": a.severity,
            "title": a.title,
            "message": a.message,
            "task_id": str(a.task_id) if a.task_id else None,
            "created_at": a.created_at.isoformat(),
            "dismissed_at": a.dismissed_at.isoformat() if a.dismissed_at else None,
        }
        for a in alerts
    ]


@router.post("/alerts/{alert_id}/dismiss")
async def dismiss_alert(
    alert_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    alert = await session.get(Alert, uuid.UUID(alert_id))
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    # Verify alert belongs to user's project (via membership)
    member = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == alert.project_id,
                ProjectMember.user_id == current_user["user_id"],
            )
        )
    ).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.dismissed_at = _utcnow()
    session.add(alert)
    await session.commit()
    return {"status": "dismissed"}


@router.post("/alerts/dismiss-all")
async def dismiss_all_alerts(
    project_id: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    stmt = update(Alert).where(
        Alert.dismissed_at.is_(None),
        Alert.project_id.in_(_user_project_ids(current_user["user_id"])),
    )
    if project_id:
        stmt = stmt.where(Alert.project_id == project_id)
    stmt = stmt.values(dismissed_at=_utcnow())
    result = await session.execute(stmt)
    await session.commit()
    return {"dismissed_count": result.rowcount}


# ── Notification Dispatch Stats Endpoint ─────────────────────────────────────


@router.get("/notification-dispatch/stats")
async def get_notification_dispatch_stats(
    project_id: str = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return webhook delivery stats for the project (success/failure counts, retry queue depth)."""
    try:
        pid = uuid.UUID(project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    await _get_user_project_with_role(pid, current_user["user_id"], session, "member")
    return _nd.get_notification_stats(project_id=pid)


# ── Project Message Endpoints ─────────────────────────────────────────────────


@router.get("/messages")
async def list_messages(
    project_id: Optional[str] = Query(None),
    status: Optional[str] = Query("open"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """List project messages, optionally filtered by project and status."""
    user_pids = _user_project_ids(current_user["user_id"])
    filters = [ProjectMessage.project_id.in_(user_pids)]
    if project_id:
        try:
            filters.append(ProjectMessage.project_id == uuid.UUID(project_id))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid project_id format")
    if status and status != "all":
        if status not in ("open", "resolved"):
            raise HTTPException(
                status_code=400,
                detail="Invalid status filter. Must be 'open', 'resolved', or 'all'",
            )
        filters.append(ProjectMessage.status == status)

    total = (
        await session.execute(
            select(func.count()).select_from(ProjectMessage).where(*filters)
        )
    ).scalar() or 0

    # Guarantee open high-signal messages are always returned regardless of routine volume.
    # Covers: user-authored, BLOCKER/FEEDBACK/BUG_REPORT, and system NOTE messages whose
    # event_type is classified as high_signal in the event-type snapshot.
    guaranteed_msgs: list = []
    if status != "resolved":
        pid_uuid = uuid.UUID(project_id) if project_id else None
        # Cap at 100 to prevent unbounded result sets when high-signal volume is abnormally
        # high. Clients should treat the returned list as "up to 100 most recent" rather
        # than a guaranteed-complete set.
        _HS_LIMIT = 100
        hs_q = (
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id.in_(user_pids),
                ProjectMessage.status == "open",
                or_(
                    ProjectMessage.from_type == "user",
                    ProjectMessage.message_type.in_(
                        ["FEEDBACK", "BLOCKER", "BUG_REPORT"]
                    ),
                    and_(
                        ProjectMessage.message_type == MessageType.NOTE,
                        ProjectMessage.from_type == "system",
                        ProjectMessage.context["event_type"]
                        .as_string()
                        .in_(list(HIGH_SIGNAL_NOTE_EVENT_TYPES)),
                    ),
                ),
                *([ProjectMessage.project_id == pid_uuid] if pid_uuid else []),
            )
            .order_by(ProjectMessage.created_at.desc())
            .limit(_HS_LIMIT)
        )
        guaranteed_msgs = list((await session.execute(hs_q)).scalars().all())

    result = await session.execute(
        select(ProjectMessage)
        .where(*filters)
        .order_by(ProjectMessage.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    paged = list(result.scalars().all())
    guaranteed_ids = {m.id for m in guaranteed_msgs}
    messages = guaranteed_msgs + [m for m in paged if m.id not in guaranteed_ids]

    return {
        "messages": [
            {
                "id": str(m.id),
                "project_id": str(m.project_id),
                "message_type": m.message_type,
                "from_type": m.from_type,
                "from_task_id": str(m.from_task_id) if m.from_task_id else None,
                "content": m.content,
                "context": m.context or {},
                "status": m.status,
                "resolution": m.resolution,
                "resolved_at": m.resolved_at.isoformat() if m.resolved_at else None,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in messages
        ],
        "total": total,
    }


@router.post("/messages")
async def create_message(
    payload: MessageCreate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """User creates a message (feedback, bug report, note)."""
    try:
        project_id = uuid.UUID(payload.project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    # Verify user has access. The returned instance is only used for the
    # access check; the routine-digest branch below re-reads the project under
    # a row lock so its read-append-write on running_update is concurrency-safe.
    await _get_user_project(project_id, current_user["user_id"], session)

    valid_types = {t.value for t in MessageType}
    msg_type = payload.message_type.upper()
    if msg_type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid message_type. Must be one of: {', '.join(valid_types)}",
        )
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Content cannot be empty")
    if len(content) > MESSAGE_CONTENT_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"Content exceeds {MESSAGE_CONTENT_MAX} character limit",
        )
    # Cap context size to prevent storage abuse
    ctx = payload.context or {}
    if len(json.dumps(ctx)) > 50_000:
        raise HTTPException(status_code=400, detail="Context too large (max 50KB)")

    msg = ProjectMessage(
        project_id=project_id,
        message_type=MessageType(msg_type),
        from_type="user",
        content=content,
        context=ctx,
    )
    session.add(msg)
    event_type = ctx.get("event_type")
    if event_type and event_type in ROUTINE_EVENT_TYPES:
        # The routine-digest append is a read-modify-write on running_update,
        # which races with concurrent message creates and with the agent's
        # locked set_running_update reconciliation. Acquire the project row
        # lock (matching the codebase's existing idiom) so the append reads a
        # fresh, committed running_update and serializes against other writers;
        # without it, a stale read could silently drop a digest line or clobber
        # the agent's reconciled document. populate_existing on the locked read
        # refreshes the identity-mapped project loaded by _get_user_project.
        locked_project = await lock_project_or_429(session, project_id)
        append_routine_digest(locked_project, event_type, content)
    await session.commit()
    await session.refresh(msg)

    # Wake the strategist on genuine operator steering. This is the live-chat
    # surface the admin MessagesPanel composer hits, and it must steer exactly
    # like the project-scoped CLI endpoint (routers/directions.py). The shared
    # gate decides whether to arm (non-passive type, not a routine digest event)
    # so the two paths cannot diverge again. Best-effort: the helper does not
    # commit (we own the txn) and swallows its own errors, so message creation
    # never fails on it; the second commit persists the marker independently.
    from app.strategic_trigger import (
        classify_operator_arming,
        maybe_arm_operator_refresh,
    )

    # Classify first so the response can report the *actual* wake decision the
    # operator's message triggered (steering / passive note / routine event).
    # maybe_arm_operator_refresh derives its arm/no-arm choice from the same
    # classifier, so the surfaced ``arming`` value always matches what arming
    # actually did. Additive field — existing ``id``/``status`` are unchanged.
    arming = classify_operator_arming(MessageType(msg_type), event_type=event_type)
    if await maybe_arm_operator_refresh(
        session, project_id, MessageType(msg_type), event_type=event_type
    ):
        await session.commit()
    return {"id": str(msg.id), "status": "created", "arming": arming}


@router.patch("/messages/{message_id}")
async def resolve_message(
    message_id: uuid.UUID,
    payload: MessageResolve,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Resolve a message with an optional response."""
    msg = await session.get(ProjectMessage, message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    # Verify user has access to the project
    await _get_user_project(msg.project_id, current_user["user_id"], session)

    if msg.status == "resolved":
        return {"id": str(msg.id), "status": "already_resolved"}
    resolution = (payload.resolution or "").strip()
    if len(resolution) > MESSAGE_CONTENT_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"Content exceeds {MESSAGE_CONTENT_MAX} character limit",
        )
    msg.status = "resolved"
    msg.resolution = resolution or None
    msg.resolved_at = _utcnow()
    session.add(msg)
    await session.commit()
    return {"id": str(msg.id), "status": "resolved"}


@router.delete("/messages/{message_id}")
async def delete_message(
    message_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Delete a message."""
    msg = await session.get(ProjectMessage, message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    await _get_user_project(msg.project_id, current_user["user_id"], session)
    await session.delete(msg)
    await session.commit()
    return {"deleted": True}


# ── Messages Last-Seen Endpoints ─────────────────────────────────────────────


@router.get("/messages/last-seen")
async def get_messages_last_seen(
    project_id: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the operator's last-seen timestamp (ms epoch) for the messages panel divider.

    When project_id is omitted, returns the maximum last-seen across all the operator's
    projects — useful for the Catch-up digest panel which shows activity cross-project.
    """
    if project_id is None:
        # Cross-project max: same store as per-project last-seen, used by CatchUp digest
        max_ts = (
            await session.execute(
                select(func.max(MessagesLastSeen.last_seen_at)).where(
                    MessagesLastSeen.user_id == current_user["user_id"],
                )
            )
        ).scalar_one_or_none()
        return {"last_seen_at": int(max_ts.timestamp() * 1000) if max_ts else None}

    try:
        pid = uuid.UUID(project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    await _get_user_project(pid, current_user["user_id"], session)

    row = (
        await session.execute(
            select(MessagesLastSeen).where(
                MessagesLastSeen.user_id == current_user["user_id"],
                MessagesLastSeen.project_id == pid,
            )
        )
    ).scalar_one_or_none()

    if row is None:
        return {"last_seen_at": None}
    return {"last_seen_at": int(row.last_seen_at.timestamp() * 1000)}


@router.put("/messages/last-seen")
async def put_messages_last_seen(
    payload: LastSeenUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Upsert the operator's last-seen timestamp (ms epoch) for the messages panel divider."""
    try:
        pid = uuid.UUID(payload.project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    await _get_user_project(pid, current_user["user_id"], session)

    new_ts = datetime.fromtimestamp(
        payload.timestamp / 1000.0, tz=timezone.utc
    ).replace(tzinfo=None)

    row = (
        await session.execute(
            select(MessagesLastSeen).where(
                MessagesLastSeen.user_id == current_user["user_id"],
                MessagesLastSeen.project_id == pid,
            )
        )
    ).scalar_one_or_none()

    if row is None:
        row = MessagesLastSeen(
            user_id=current_user["user_id"],
            project_id=pid,
            last_seen_at=new_ts,
            updated_at=new_ts,
        )
    else:
        row.last_seen_at = new_ts
        row.updated_at = _utcnow()

    session.add(row)
    await session.commit()
    return {"status": "ok", "last_seen_at": int(new_ts.timestamp() * 1000)}


# ── Messages Noise-Prefs Endpoints ───────────────────────────────────────────


@router.get("/messages/noise-prefs")
async def get_messages_noise_prefs(
    project_id: str = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the operator's messages-panel noise-control preferences."""
    try:
        pid = uuid.UUID(project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    await _get_user_project(pid, current_user["user_id"], session)

    row = (
        await session.execute(
            select(MessagesNoisePrefs).where(
                MessagesNoisePrefs.user_id == current_user["user_id"],
                MessagesNoisePrefs.project_id == pid,
            )
        )
    ).scalar_one_or_none()

    return {"prefs": row.prefs if row is not None else {}}


@router.put("/messages/noise-prefs")
async def put_messages_noise_prefs(
    payload: NoisePrefUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Upsert the operator's messages-panel noise-control preferences."""
    try:
        pid = uuid.UUID(payload.project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    await _get_user_project(pid, current_user["user_id"], session)

    if len(json.dumps(payload.prefs)) > 50_000:
        raise HTTPException(status_code=400, detail="Prefs too large (max 50KB)")

    row = (
        await session.execute(
            select(MessagesNoisePrefs).where(
                MessagesNoisePrefs.user_id == current_user["user_id"],
                MessagesNoisePrefs.project_id == pid,
            )
        )
    ).scalar_one_or_none()

    # This endpoint only owns event-type visibility prefs. Strip any reserved
    # keys from the incoming payload (they are owned by the temp-mutes and
    # mute-window endpoints) and preserve the existing reserved values so a
    # visibility save never clobbers an operator's active DND window or mutes.
    visibility = {
        k: v for k, v in payload.prefs.items() if k not in _RESERVED_PREFS_KEYS
    }

    if row is None:
        row = MessagesNoisePrefs(
            user_id=current_user["user_id"],
            project_id=pid,
            prefs=visibility,
        )
    else:
        existing = dict(row.prefs or {})
        merged = dict(visibility)
        for key in _RESERVED_PREFS_KEYS:
            if key in existing:
                merged[key] = existing[key]
        row.prefs = merged
        flag_modified(row, "prefs")

    session.add(row)
    await session.commit()
    return {"status": "ok"}


# ── Messages Saved-Views Endpoints ──────────────────────────────────────────


@router.get("/messages/saved-views")
async def get_messages_saved_views(
    project_id: str = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the operator's saved views for the messages panel."""
    try:
        pid = uuid.UUID(project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    await _get_user_project(pid, current_user["user_id"], session)

    row = (
        await session.execute(
            select(MessagesSavedViews).where(
                MessagesSavedViews.user_id == current_user["user_id"],
                MessagesSavedViews.project_id == pid,
            )
        )
    ).scalar_one_or_none()

    return {"saved_views": row.saved_views if row is not None else []}


@router.put("/messages/saved-views")
async def put_messages_saved_views(
    payload: SavedViewsUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Upsert the operator's saved views for the messages panel."""
    try:
        pid = uuid.UUID(payload.project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    await _get_user_project(pid, current_user["user_id"], session)

    if len(json.dumps(payload.saved_views)) > 50_000:
        raise HTTPException(status_code=400, detail="Saved views too large (max 50KB)")

    row = (
        await session.execute(
            select(MessagesSavedViews).where(
                MessagesSavedViews.user_id == current_user["user_id"],
                MessagesSavedViews.project_id == pid,
            )
        )
    ).scalar_one_or_none()

    if row is None:
        row = MessagesSavedViews(
            user_id=current_user["user_id"],
            project_id=pid,
            saved_views=payload.saved_views,
        )
    else:
        row.saved_views = payload.saved_views
        flag_modified(row, "saved_views")

    session.add(row)
    await session.commit()
    return {"status": "ok"}


# ── Messages Temp-Mutes Endpoints ────────────────────────────────────────────


@router.get("/messages/temp-mutes")
async def get_messages_temp_mutes(
    project_id: str = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return active (non-expired) temporary event-type mutes for the operator."""
    try:
        pid = uuid.UUID(project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    await _get_user_project(pid, current_user["user_id"], session)

    row = (
        await session.execute(
            select(MessagesNoisePrefs).where(
                MessagesNoisePrefs.user_id == current_user["user_id"],
                MessagesNoisePrefs.project_id == pid,
            )
        )
    ).scalar_one_or_none()

    raw: Dict[str, int] = (row.prefs.get(_TEMP_MUTES_KEY) or {}) if row else {}
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    return {"temp_mutes": {et: exp for et, exp in raw.items() if exp > now_ms}}


@router.put("/messages/temp-mutes")
async def put_messages_temp_mutes(
    payload: TempMutesUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Upsert temporary event-type mutes for the operator. Expired entries are dropped."""
    try:
        pid = uuid.UUID(payload.project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    await _get_user_project(pid, current_user["user_id"], session)

    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    active = {et: exp for et, exp in payload.temp_mutes.items() if exp > now_ms}

    row = (
        await session.execute(
            select(MessagesNoisePrefs).where(
                MessagesNoisePrefs.user_id == current_user["user_id"],
                MessagesNoisePrefs.project_id == pid,
            )
        )
    ).scalar_one_or_none()

    if row is None:
        row = MessagesNoisePrefs(
            user_id=current_user["user_id"],
            project_id=pid,
            prefs={_TEMP_MUTES_KEY: active},
        )
    else:
        prefs = dict(row.prefs or {})
        prefs[_TEMP_MUTES_KEY] = active
        row.prefs = prefs
        flag_modified(row, "prefs")

    session.add(row)
    await session.commit()
    return {"status": "ok"}


# ── Messages Mute-Window Endpoints ────────────────────────────────────────────


@router.get("/messages/mute-window")
async def get_messages_mute_window(
    project_id: str = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the active do-not-disturb mute window for the operator, or null if expired/unset."""
    try:
        pid = uuid.UUID(project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    await _get_user_project(pid, current_user["user_id"], session)

    row = (
        await session.execute(
            select(MessagesNoisePrefs).where(
                MessagesNoisePrefs.user_id == current_user["user_id"],
                MessagesNoisePrefs.project_id == pid,
            )
        )
    ).scalar_one_or_none()

    stored = (row.prefs.get(_MUTE_WINDOW_KEY)) if row else None
    if not stored or not isinstance(stored, dict):
        return {"mute_window": None}
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    if stored.get("expires_at", 0) <= now_ms:
        return {"mute_window": None}
    return {"mute_window": stored}


@router.put("/messages/mute-window")
async def put_messages_mute_window(
    payload: MuteWindowUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Upsert the do-not-disturb mute window. Set expires_at=null to clear it."""
    try:
        pid = uuid.UUID(payload.project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    await _get_user_project(pid, current_user["user_id"], session)

    entry = (
        {"expires_at": payload.expires_at} if payload.expires_at is not None else None
    )

    row = (
        await session.execute(
            select(MessagesNoisePrefs).where(
                MessagesNoisePrefs.user_id == current_user["user_id"],
                MessagesNoisePrefs.project_id == pid,
            )
        )
    ).scalar_one_or_none()

    if row is None:
        row = MessagesNoisePrefs(
            user_id=current_user["user_id"],
            project_id=pid,
            prefs={_MUTE_WINDOW_KEY: entry},
        )
    else:
        prefs = dict(row.prefs or {})
        prefs[_MUTE_WINDOW_KEY] = entry
        row.prefs = prefs
        flag_modified(row, "prefs")

    session.add(row)
    await session.commit()
    return {"status": "ok"}


# ── Last-Visit-Timestamp Endpoints ───────────────────────────────────────────


@router.get("/last-visit-timestamp")
async def get_last_visit_timestamp(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Most-recent per-operator last-visit timestamp across all projects.

    Reuses MessagesLastSeen so ActivityFeed and CatchUpPanel share the same
    cross-device baseline as the Messages panel without a separate store.
    """
    max_ts = (
        await session.execute(
            select(func.max(MessagesLastSeen.last_seen_at)).where(
                MessagesLastSeen.user_id == current_user["user_id"],
            )
        )
    ).scalar_one_or_none()
    return {"last_visit_at": int(max_ts.timestamp() * 1000) if max_ts else None}


@router.put("/last-visit-timestamp")
async def put_last_visit_timestamp(
    payload: LastSeenUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Persist the per-operator last-visit timestamp for cross-device home-panel baseline.

    Writes to MessagesLastSeen (same store as the Messages panel last-seen
    divider) so a single authoritative record drives all 'since last visit' UI.
    """
    try:
        pid = uuid.UUID(payload.project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    await _get_user_project(pid, current_user["user_id"], session)
    new_ts = datetime.fromtimestamp(
        payload.timestamp / 1000.0, tz=timezone.utc
    ).replace(tzinfo=None)
    row = (
        await session.execute(
            select(MessagesLastSeen).where(
                MessagesLastSeen.user_id == current_user["user_id"],
                MessagesLastSeen.project_id == pid,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = MessagesLastSeen(
            user_id=current_user["user_id"],
            project_id=pid,
            last_seen_at=new_ts,
            updated_at=new_ts,
        )
    else:
        row.last_seen_at = new_ts
        row.updated_at = _utcnow()
    session.add(row)
    await session.commit()
    return {"status": "ok", "last_visit_at": int(new_ts.timestamp() * 1000)}


# ── Admin Playbook Endpoints ──────────────────────────────────────────────────


@router.get("/projects/{project_id}/playbook")
@limiter.limit("60/minute")
async def admin_playbook_list(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """List all playbook entries for a project with full metadata."""
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    doc = project.design_doc or {}
    entries = {}
    for k, v in doc.items():
        if k.startswith("playbook/") and isinstance(v, dict):
            short_key = k[len("playbook/") :]
            last_referenced_at = v.get("last_referenced_at")
            updated_at = v.get("updated_at", "")
            # Compute stale: stored flag (auto-archived for low effectiveness) OR no reference in 30 days
            now_utc = _utcnow()
            _STALE_DAYS = 30
            stale = bool(v.get("stale"))  # Stored flag (auto-archived) takes precedence
            if not stale:
                if last_referenced_at:
                    try:
                        from datetime import datetime

                        ts = datetime.fromisoformat(last_referenced_at)
                        if ts.tzinfo is not None:
                            ts = ts.replace(tzinfo=None)
                        stale = (now_utc - ts).days >= _STALE_DAYS
                    except Exception:
                        logger.exception(
                            f"[PLAYBOOK] Failed to parse last_referenced_at={last_referenced_at}"
                        )
                elif not v.get("reference_count", 0):
                    # Never referenced: use updated_at as staleness proxy
                    if updated_at:
                        try:
                            from datetime import datetime

                            ts = datetime.fromisoformat(updated_at)
                            if ts.tzinfo is not None:
                                ts = ts.replace(tzinfo=None)
                            stale = (now_utc - ts).days >= _STALE_DAYS
                        except Exception:
                            logger.exception(
                                f"[PLAYBOOK] Failed to parse updated_at={updated_at}"
                            )
                    else:
                        stale = True
                # reference_count > 0 but no last_referenced_at → unknown recency, not stale
            age_days = 0
            if updated_at:
                try:
                    from datetime import datetime as _dt

                    _ts = _dt.fromisoformat(updated_at.replace("Z", "+00:00"))
                    if _ts.tzinfo is not None:
                        _ts = _ts.replace(tzinfo=None)
                    age_days = (now_utc - _ts).days
                except Exception:
                    pass
            confirmed = v.get("confirmed_count", 0)
            referenced = v.get("reference_count", 0)
            entries[short_key] = {
                "content": v.get("content", ""),
                "source_task_id": v.get("source_task_id"),
                "updated_at": updated_at,
                "confirmed_count": confirmed,
                "applicable_types": v.get("applicable_types") or [],
                "reference_count": referenced,
                "last_referenced_at": last_referenced_at,
                "last_confirmed_at": v.get("last_confirmed_at"),
                "effectiveness_score": round(confirmed / max(referenced, 1), 3)
                if (confirmed > 0 or referenced > 0)
                else v.get("effectiveness_score"),
                "stale": stale,
                "source": v.get("source", ""),
                "age_days": age_days,
            }
    return {"entries": entries, "count": len(entries)}


@router.put("/projects/{project_id}/playbook")
@limiter.limit("30/minute")
async def admin_playbook_set(
    request: Request,
    project_id: uuid.UUID,
    payload: AdminPlaybookSet,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Create or update a playbook entry."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    key = payload.key.strip()
    if not _ADMIN_PLAYBOOK_KEY_RE.match(key):
        raise HTTPException(
            status_code=400,
            detail="Invalid key: must match ^[a-zA-Z0-9][a-zA-Z0-9\\-]{0,49}$",
        )
    if len(payload.value) > 2048:
        raise HTTPException(
            status_code=400,
            detail=f"Value too large ({len(payload.value)} bytes, max 2048)",
        )
    if payload.applicable_types is not None:
        invalid = [t for t in payload.applicable_types if t not in _VALID_TASK_TYPES]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid task type(s): {invalid}. Must be one of: {sorted(_VALID_TASK_TYPES)}",
            )

    # Phase A1: skip_locked + 429 on contention so 30s lock_timeout doesn't
    # propagate as 500 and trigger Cloud Run instance quarantine.
    project = await lock_project_or_429(session, project_id)

    doc = dict(project.design_doc or {})
    playbook_key = f"playbook/{key}"
    existing = doc.get(playbook_key, {})
    new_entry: dict = {
        "content": payload.value,
        "source_task_id": None,
        "updated_at": _utcnow().isoformat(),
        "confirmed_count": existing.get("confirmed_count", 0)
        if isinstance(existing, dict)
        else 0,
    }
    if payload.applicable_types is not None:
        new_entry["applicable_types"] = payload.applicable_types

    # Evict least-useful entries until the incoming write fits within capacity.
    # Loop so a single large write or an already-over-cap playbook self-heals.
    entry_estimate = len(json.dumps(new_entry).encode())
    while _playbook_size_bytes(doc) + entry_estimate > _PLAYBOOK_CAPACITY_BYTES:
        size_before = _playbook_size_bytes(doc)
        doc = _archive_least_useful_entry(doc)
        if _playbook_size_bytes(doc) == size_before:
            # No candidates were evictable — hard cap as final backstop.
            playbook_total = _playbook_size_bytes(doc)
            raise HTTPException(
                status_code=400,
                detail=f"Playbook too large ({playbook_total} bytes, max {_PLAYBOOK_CAPACITY_BYTES} bytes total) and no entries are evictable",
            )

    doc[playbook_key] = new_entry

    meta = doc.get("_meta", {"revision": 0})
    meta["revision"] = meta.get("revision", 0) + 1
    meta["last_updated"] = _utcnow().isoformat()
    meta["updated_by"] = "admin"
    doc["_meta"] = meta

    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)
    await session.commit()

    return {"status": "ok", "key": key, "revision": meta["revision"]}


@router.delete("/projects/{project_id}/playbook/{key}")
@limiter.limit("30/minute")
async def admin_playbook_delete(
    request: Request,
    project_id: uuid.UUID,
    key: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Delete a playbook entry by key."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    # Phase A1: skip_locked + 429 on contention so 30s lock_timeout doesn't
    # propagate as 500 and trigger Cloud Run instance quarantine.
    project = await lock_project_or_429(session, project_id)

    doc = dict(project.design_doc or {})
    playbook_key = f"playbook/{key}"
    if playbook_key not in doc:
        raise HTTPException(status_code=404, detail=f"Playbook key '{key}' not found")

    del doc[playbook_key]

    # Record a tombstone so auto-playbook regeneration stays suppressed even
    # after archive trimming.
    record_playbook_tombstone(doc, playbook_key, archived_at=_utcnow().isoformat())

    meta = doc.get("_meta", {"revision": 0})
    meta["revision"] = meta.get("revision", 0) + 1
    meta["last_updated"] = _utcnow().isoformat()
    meta["updated_by"] = "admin"
    doc["_meta"] = meta

    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)
    await session.commit()

    return {"status": "ok", "key": key, "deleted": True}


@router.post("/projects/{project_id}/playbook/seed")
@limiter.limit("10/minute")
async def admin_playbook_seed(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Populate the playbook with default operational knowledge. Skips entries that already exist."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    # Phase A1: skip_locked + 429 on contention so 30s lock_timeout doesn't
    # propagate as 500 and trigger Cloud Run instance quarantine.
    project = await lock_project_or_429(session, project_id)

    doc = dict(project.design_doc or {})
    added = []
    skipped = []

    for key, entry in _SEED_ENTRIES.items():
        playbook_key = f"playbook/{key}"
        if playbook_key in doc:
            skipped.append(key)
            continue
        seed_entry: dict = {
            "content": entry["content"],
            "source_task_id": None,
            "updated_at": _utcnow().isoformat(),
            "confirmed_count": 0,
        }
        if entry.get("applicable_types") is not None:
            seed_entry["applicable_types"] = entry["applicable_types"]
        doc[playbook_key] = seed_entry
        added.append(key)

    if added:
        meta = doc.get("_meta", {"revision": 0})
        meta["revision"] = meta.get("revision", 0) + 1
        meta["last_updated"] = _utcnow().isoformat()
        meta["updated_by"] = "admin-seed"
        doc["_meta"] = meta

        project.design_doc = doc
        flag_modified(project, "design_doc")
        session.add(project)
        await session.commit()

    return {"status": "ok", "added": added, "skipped": skipped}
