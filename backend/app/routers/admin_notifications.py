"""Alert, message, and playbook management endpoints."""

import logging
import re
import uuid
import json
import math
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, update, or_, and_
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import get_session
from app.broadcast import broadcast_to_project
from app.models import (
    Alert,
    Project,
    ProjectMember,
    ProjectMessage,
    MessageType,
    MESSAGE_CONTENT_MAX,
    MessagesLastSeen,
    MessagesSavedViews,
    _utcnow,
)
from app.auth import get_current_user
from app.shared_state import limiter
from app.admin import (
    _ROLE_LEVEL,
    _get_user_project,
    _get_user_project_with_role,
    _member_project_ids,
    _user_project_ids,
)
from app.task_locking import lock_project_or_429
from app.message_convergence import LINKED_STATUS
from app.project_message_creation import (
    create_operator_project_message,
    parse_operator_message_type,
)
from app.project_message_projection import (
    admin_message_dict,
    admin_message_list_dict,
    message_event_payload,
)
from app.messages_noise_prefs_store import (
    get_messages_noise_prefs_row,
    mutate_messages_noise_prefs,
)
from app.messages_last_seen import (
    naive_utc_to_epoch_ms,
    timestamp_ms_to_naive_utc,
    upsert_messages_last_seen,
)
from app.workbench_state import (
    WORKBENCH_STATE_KEY, WORKBENCH_STATE_MAX_BYTES,
    build_message_strategic_session_snapshot, get_strategic_workbench_state,
    persist_strategic_workbench_update,
    workbench_context_for_strategic_session_snapshot,
    workbench_state_exceeds_max_bytes, workbench_state_size_bytes,
)
from app.workbench_events import (
    SURFACE_MESSAGES,
    SURFACE_RUNTIME,
    SURFACE_STRATEGIC_SESSION,
    emit_workbench_refetch,
)
from app.strategic_trigger import (
    ARMING_STEERING,
    maybe_arm_operator_refresh,
)
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


class _MessageCreate(BaseModel):
    project_id: str
    message_type: str = "NOTE"
    content: str
    context: Optional[Dict[str, Any]] = None
    workbench_context: Optional[Dict[str, Any]] = None
    idempotency_key: Optional[str] = None


class _MessageResolve(BaseModel):
    resolution: Optional[str] = None
    workbench_context: Optional[Dict[str, Any]] = None


class _LastSeenUpdate(BaseModel):
    project_id: str
    timestamp: int  # Unix ms epoch


class _NoisePrefUpdate(BaseModel):
    project_id: str
    prefs: Dict[str, Any] = Field(default_factory=dict)


class _SavedViewsUpdate(BaseModel):
    project_id: str
    saved_views: List[Any] = Field(default_factory=list)


class _TempMutesUpdate(BaseModel):
    project_id: str
    temp_mutes: Dict[str, int] = Field(
        default_factory=dict
    )  # event_type -> expiry ms epoch


class _MuteWindowUpdate(BaseModel):
    project_id: str
    expires_at: Optional[int] = None  # ms epoch; None clears the mute window


class _WorkbenchStateUpdate(BaseModel):
    project_id: str
    workbench: Dict[str, Any] = Field(default_factory=dict)


class _AdminPlaybookSet(BaseModel):
    key: str = Field(
        ..., description="Playbook key: alphanumeric + hyphens, max 50 chars"
    )
    value: str = Field(..., description="Playbook value: plain text, max 2048 bytes")
    applicable_types: Optional[List[str]] = Field(
        None, description="Task types this entry applies to; None = shown to all types"
    )


# ── Constants ─────────────────────────────────────────────────────────────────

_ADMIN_PLAYBOOK_KEY_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]{0,49}$")
_MESSAGES_SAVED_VIEWS_CAP = 10
_MESSAGES_SAVED_VIEW_PREF_VALUES = {"mute", "routine", "surfaced"}


def _sanitize_message_saved_view(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None

    view_id = raw.get("id")
    name = raw.get("name")
    if not isinstance(view_id, str) or not view_id:
        return None
    if not isinstance(name, str) or not name:
        return None

    prefs_raw = raw.get("prefs")
    prefs: Dict[str, str] = {}
    if isinstance(prefs_raw, dict):
        for key, value in prefs_raw.items():
            if (
                isinstance(key, str)
                and isinstance(value, str)
                and value in _MESSAGES_SAVED_VIEW_PREF_VALUES
            ):
                prefs[key] = value

    created_at = raw.get("createdAt")
    if not isinstance(created_at, (int, float)) or isinstance(created_at, bool) or not math.isfinite(created_at):
        created_at = int(datetime.now(timezone.utc).timestamp() * 1000)

    saved_view: Dict[str, Any] = {
        "id": view_id,
        "name": name,
        "query": raw.get("query") if isinstance(raw.get("query"), str) else "",
        "prefs": prefs,
        "sort": "recent" if raw.get("sort") == "recent" else "importance",
        "showResolved": raw.get("showResolved") is True,
        "routineExpanded": raw.get("routineExpanded") is True,
        "showOnlyNew": raw.get("showOnlyNew") is True,
        "createdAt": created_at,
    }
    description = raw.get("description")
    if isinstance(description, str):
        saved_view["description"] = description
    return saved_view


def _sanitize_message_saved_views(saved_views: Any) -> List[Dict[str, Any]]:
    if not isinstance(saved_views, list):
        return []

    sanitized: List[Dict[str, Any]] = []
    for raw in saved_views:
        saved_view = _sanitize_message_saved_view(raw)
        if saved_view is None:
            continue
        sanitized.append(saved_view)
        if len(sanitized) >= _MESSAGES_SAVED_VIEWS_CAP:
            break
    return sanitized

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
        "direction_staged_activated",
        "promotion_readiness_reached",
        "release_calcifying",
        "release_build_stalled",
        "release_stabilize_stalled",
        "release_ci_unresolved_stall",
        "release_auto_merge_stalled",
        "strategic_steering_deferred",
        "strategic_steering_acted",
        "capability_trial_stalled",
        "capability_verdict_decided",
        "capability_trial_ambiguous_lift_escalation",
        "criterion_retracted",
        "criterion_superseded",
        "question_answer",
    }
)


# Reserved keys within MessagesNoisePrefs.prefs for temp-mutes and mute-window.
# Prefixed with underscore to avoid collision with event-type visibility keys.
_TEMP_MUTES_KEY = "_temp_mutes"
_MUTE_WINDOW_KEY = "_mute_window"
# These keys are owned by their own dedicated PUT endpoints; the noise-prefs
# (event-visibility) endpoint must never write them, only preserve them.
_RESERVED_PREFS_KEYS = (_TEMP_MUTES_KEY, _MUTE_WINDOW_KEY, WORKBENCH_STATE_KEY)


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
    domain: Optional[str] = None,
    event_type: Optional[str] = None,
    source: Optional[str] = None,
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
    if domain:
        query = query.where(Alert.domain == domain)
    if event_type:
        query = query.where(Alert.event_type == event_type)
    if source:
        query = query.where(Alert.source == source)
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
            "domain": a.domain,
            "event_type": a.event_type,
            "source": a.source,
            "dedupe_key": a.dedupe_key,
            "occurrence_count": a.occurrence_count,
            "last_seen_at": a.last_seen_at.isoformat() if a.last_seen_at else None,
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
    # Verify alert belongs to user's project (via membership) with member+
    # role (viewers are read-only and may not mutate alert state)
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
    if _ROLE_LEVEL.get(member.role, 0) < _ROLE_LEVEL["member"]:
        raise HTTPException(
            status_code=403, detail="Requires member role on this project"
        )
    alert.dismissed_at = _utcnow()
    session.add(alert)
    await session.commit()
    await emit_workbench_refetch(
        alert.project_id,
        resource="alert",
        action="dismissed",
        refetch_surfaces=(SURFACE_RUNTIME, SURFACE_STRATEGIC_SESSION, SURFACE_MESSAGES),
        summary_key="alert",
        summary={
            "alert_id": str(alert.id),
            "severity": getattr(alert.severity, "value", alert.severity),
            "title": alert.title,
        },
        alert_id=str(alert.id),
    )
    return {"status": "dismissed"}


@router.post("/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(
    alert_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Acknowledge an ongoing alert, suppressing further escalation re-pages.

    Sets ``acknowledged_at`` on the alert without dismissing it — the alert
    remains visible in the operator UI, but the orchestrator's escalation sweep
    will not fire additional webhooks for this outage until the alert auto-resolves
    on recovery (e.g. runner comes back online). Used primarily for runner-offline
    escalation suppression: operator sees the alert, knows about it, and opts out
    of repeated pages while the issue is being worked. Idempotent — acknowledging
    an already-acknowledged alert is a no-op that returns 200.
    """
    alert = await session.get(Alert, uuid.UUID(alert_id))
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
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
    if _ROLE_LEVEL.get(member.role, 0) < _ROLE_LEVEL["member"]:
        raise HTTPException(
            status_code=403, detail="Requires member role on this project"
        )
    if alert.acknowledged_at is None:
        alert.acknowledged_at = _utcnow()
        session.add(alert)
        await session.commit()
    return {"status": "acknowledged"}


@router.post("/alerts/dismiss-all")
async def dismiss_all_alerts(
    project_id: Optional[str] = None,
    domain: Optional[str] = None,
    event_type: Optional[str] = None,
    source: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Dismiss alerts in bulk. Only projects where the caller is member+ are
    mutated — viewers are read-only and may not silence operator escalation.

    Two shapes, deliberately different:

    * **Explicitly targeted** (``project_id`` supplied) — a 403 if the caller
      lacks member+ on it. An explicitly-targeted mutation that silently no-ops
      (200 / ``dismissed_count: 0``) tells the caller nothing about WHY nothing
      happened; gates must emit their reason.
    * **Unfiltered bulk** ("dismiss everything I can") — silent scoping is
      CORRECT: a caller who is member on 3 projects and viewer on a 4th should
      dismiss the 3, not have the whole call rejected. ``skipped_count`` reports
      what was left behind rather than hiding it.
    """
    user_id = current_user["user_id"]
    pid: uuid.UUID | None = None
    if project_id:
        try:
            pid = uuid.UUID(project_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid project_id format")
        # Explicit target → say why, don't silently dismiss nothing.
        await _get_user_project_with_role(pid, user_id, session, "member")

    filters = [Alert.dismissed_at.is_(None)]
    if pid is not None:
        filters.append(Alert.project_id == pid)
    if domain:
        filters.append(Alert.domain == domain)
    if event_type:
        filters.append(Alert.event_type == event_type)
    if source:
        filters.append(Alert.source == source)

    # Alerts the caller can SEE but may not dismiss (viewer-only projects).
    # Always 0 on the explicit-target path, which 403s above.
    skipped_count = int(
        await session.scalar(
            select(func.count())
            .select_from(Alert)
            .where(
                *filters,
                Alert.project_id.in_(_user_project_ids(user_id)),
                Alert.project_id.notin_(_member_project_ids(user_id)),
            )
        )
        or 0
    )

    stmt = (
        update(Alert)
        .where(*filters, Alert.project_id.in_(_member_project_ids(user_id)))
        .values(dismissed_at=_utcnow())
    )
    result = await session.execute(stmt)
    await session.commit()
    return {"dismissed_count": result.rowcount, "skipped_count": skipped_count}


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


class _DeadLetterRedriveRequest(BaseModel):
    project_id: str
    ids: list[str] = Field(..., min_length=1, max_length=200)


@router.get("/notification-dispatch/dead-letters")
async def list_dead_letters(
    project_id: str = Query(...),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """List dead-lettered notifications for a project (newest first)."""
    try:
        pid = uuid.UUID(project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    await _get_user_project_with_role(pid, current_user["user_id"], session, "member")
    rows, total = await _nd.list_dead_letters(session, project_id=pid, limit=limit, offset=offset)
    # Mask webhook_url for non-admin callers — webhook URLs embed auth tokens
    # (Slack, Discord, PagerDuty) and must not leak to non-admin project members.
    # This matches the pattern in admin_settings._build_notification_config_response.
    if current_user.get("role") != "admin":
        for r in rows:
            r["webhook_url"] = ""
    return {"items": rows, "total": total, "limit": limit, "offset": offset}


@router.post("/notification-dispatch/dead-letters/redrive")
async def redrive_dead_letters(
    payload: _DeadLetterRedriveRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Re-drive one or more dead-lettered notifications through the retry queue.

    Each re-driven entry is re-enqueued with a fresh attempt count; on success
    the dead-letter row is deleted so it cannot be replayed twice. Returns the
    count of entries successfully re-driven.
    """
    try:
        pid = uuid.UUID(payload.project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    await _get_user_project_with_role(pid, current_user["user_id"], session, "member")

    redriven = 0
    errors: list[dict] = []
    for dl_id in payload.ids:
        try:
            uuid.UUID(dl_id)
        except (ValueError, TypeError):
            errors.append({"id": dl_id, "error": "invalid id format"})
            continue
        try:
            await _nd.redrive_dead_letter(session, dead_letter_id=dl_id, project_id=str(pid))
            redriven += 1
        except ValueError:
            errors.append({"id": dl_id, "error": "not found"})
    return {"redriven": redriven, "errors": errors}


# ── Project Message Endpoints ─────────────────────────────────────────────────


@router.get("/messages")
async def list_messages(
    project_id: Optional[str] = Query(None),
    status: Optional[str] = Query("open"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    q: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """List project messages, optionally filtered by project, status, and content search."""
    user_pids = _user_project_ids(current_user["user_id"])
    filters = [ProjectMessage.project_id.in_(user_pids)]
    if project_id:
        try:
            filters.append(ProjectMessage.project_id == uuid.UUID(project_id))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid project_id format")
    if status and status != "all":
        if status not in ("open", LINKED_STATUS, "resolved"):
            raise HTTPException(
                status_code=400,
                detail="Invalid status filter. Must be 'open', 'linked', 'resolved', or 'all'",
            )
        filters.append(ProjectMessage.status == status)
    q_stripped = (q or "").strip()
    if q_stripped:
        q_escaped = q_stripped.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        q_pattern = f"%{q_escaped}%"
        filters.append(ProjectMessage.content.ilike(q_pattern, escape="\\"))

    total = (
        await session.execute(
            select(func.count()).select_from(ProjectMessage).where(*filters)
        )
    ).scalar() or 0

    # Fetch context for list queries but strip large keys (e.g. strategic_session
    # snapshots) in admin_message_list_dict before returning. This preserves the
    # UI-needed context fields (event_type, severity, reply_to, etc.) while
    # preventing full strategic-session payloads from inflating the poll response.
    _LIST_LOAD_ONLY = load_only(
        ProjectMessage.id,
        ProjectMessage.project_id,
        ProjectMessage.message_type,
        ProjectMessage.from_type,
        ProjectMessage.from_task_id,
        ProjectMessage.content,
        ProjectMessage.context,
        ProjectMessage.status,
        ProjectMessage.resolution,
        ProjectMessage.resolved_at,
        ProjectMessage.created_at,
        raiseload=True,
    )

    # Guarantee open high-signal messages are always returned regardless of routine volume.
    # Covers: user-authored, BLOCKER/FEEDBACK/BUG_REPORT, and system NOTE messages whose
    # event_type is classified as high_signal in the event-type snapshot.
    guaranteed_msgs: list = []
    if status in (None, "open", "all"):
        pid_uuid = uuid.UUID(project_id) if project_id else None
        # Cap at 100 to prevent unbounded result sets when high-signal volume is abnormally
        # high. Clients should treat the returned list as "up to 100 most recent" rather
        # than a guaranteed-complete set.
        _HS_LIMIT = 100
        hs_filters = [
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
        ]
        if q_stripped:
            hs_filters.append(ProjectMessage.content.ilike(q_pattern, escape="\\"))
        hs_q = (
            select(ProjectMessage)
            .where(*hs_filters)
            .order_by(ProjectMessage.created_at.desc())
            .limit(_HS_LIMIT)
            .options(_LIST_LOAD_ONLY)
        )
        guaranteed_msgs = list((await session.execute(hs_q)).scalars().all())

    result = await session.execute(
        select(ProjectMessage)
        .where(*filters)
        .order_by(ProjectMessage.created_at.desc())
        .offset(offset)
        .limit(limit)
        .options(_LIST_LOAD_ONLY)
    )
    paged = list(result.scalars().all())
    guaranteed_ids = {m.id for m in guaranteed_msgs}
    messages = guaranteed_msgs + [m for m in paged if m.id not in guaranteed_ids]

    return {
        "messages": [admin_message_list_dict(m) for m in messages],
        "total": total,
    }


@router.get("/messages/counts")
async def get_message_counts(
    project_id: str = Query(...),
    last_seen_ms: Optional[int] = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return high-signal open message counts by type plus an unread total.

    Lightweight badge-poll alternative to GET /messages — returns only integers,
    no message content or previews.
    """
    try:
        pid = uuid.UUID(project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    await _get_user_project(pid, current_user["user_id"], session)

    from app.messages_last_seen import timestamp_ms_to_naive_utc
    from app.models import MessagesLastSeen
    from app.operator_attention import get_operator_message_counts

    client_last_seen_at = timestamp_ms_to_naive_utc(last_seen_ms) if last_seen_ms is not None else None
    # Merge client-supplied timestamp with server-stored last_seen so the badge
    # works correctly on a fresh device / cleared localStorage (cross-device).
    server_last_seen_at = (
        await session.execute(
            select(MessagesLastSeen.last_seen_at).where(
                MessagesLastSeen.project_id == pid,
                MessagesLastSeen.user_id == current_user["user_id"],
            )
        )
    ).scalar_one_or_none()
    if client_last_seen_at is not None and server_last_seen_at is not None:
        last_seen_at = max(client_last_seen_at, server_last_seen_at)
    else:
        last_seen_at = client_last_seen_at or server_last_seen_at
    return await get_operator_message_counts(session, pid, last_seen_at, user_id=current_user["user_id"])


@router.post("/messages")
async def create_message(
    payload: _MessageCreate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """User creates a message (feedback, bug report, note)."""
    try:
        project_id = uuid.UUID(payload.project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    # Verify user has write access (member+; viewers are read-only). The
    # returned instance is used for the access check and for resolving the
    # citation-grounding branch context below; the routine-digest branch
    # further down re-reads the project under a row lock so its
    # read-append-write on running_update is concurrency-safe.
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    result = await create_operator_project_message(
        session,
        project=project,
        user_id=current_user["user_id"],
        message_type=parse_operator_message_type(
            payload.message_type,
            default_note_for_empty=False,
        ),
        content=payload.content,
        context=payload.context,
        workbench_context=payload.workbench_context,
        idempotency_key=payload.idempotency_key,
        compact_context_size=False,
    )
    response = {
        "id": result.message_id,
        "status": result.status,
        "arming": result.arming,
        "message": admin_message_dict(result.message),
    }
    if result.message_event is not None:
        await broadcast_to_project(
            str(project_id),
            "message_created",
            result.message_event,
        )
    return response


@router.patch("/messages/{message_id}")
async def resolve_message(
    message_id: uuid.UUID,
    payload: _MessageResolve,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Resolve a message with an optional response."""
    msg = await session.get(ProjectMessage, message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    # Verify user has write access to the project (member+; viewers are read-only)
    await _get_user_project_with_role(
        msg.project_id, current_user["user_id"], session, "member"
    )

    message_project_uuid = msg.project_id
    message_type = msg.message_type
    message_id_str = str(msg.id)
    base_message_response = admin_message_dict(msg)
    was_resolved = base_message_response.get("status") == "resolved"
    message_event = message_event_payload(msg, status="resolved")
    resolution = (payload.resolution or "").strip()
    if len(resolution) > MESSAGE_CONTENT_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"Content exceeds {MESSAGE_CONTENT_MAX} character limit",
        )
    if workbench_state_exceeds_max_bytes(payload.workbench_context):
        raise HTTPException(status_code=400, detail=f"workbench_context exceeds {WORKBENCH_STATE_MAX_BYTES} byte limit")
    workbench_context_for_snapshot = payload.workbench_context
    if payload.workbench_context is not None:
        workbench_write = await persist_strategic_workbench_update(
            session,
            current_user["user_id"],
            message_project_uuid,
            payload.workbench_context,
            arm_interaction_mode=False,
            commit=False,
        )
        workbench_context_for_snapshot = workbench_context_for_strategic_session_snapshot(
            workbench_write.state,
            payload.workbench_context,
        )
    if was_resolved:
        await session.commit()
        return {"id": message_id_str, "status": "already_resolved", "message": base_message_response}
    resolved_at = _utcnow()
    # Use update with status check for idempotency: only update if still open.
    # If a concurrent request also resolves before this one commits, the update
    # will find 0 rows and we return already_resolved for both paths.
    result = await session.execute(
        update(ProjectMessage).where(
            ProjectMessage.id == message_id,
            ProjectMessage.status.in_(["open", LINKED_STATUS])
        ).values(
            status="resolved",
            resolution=resolution or None,
            resolved_at=resolved_at,
        )
    )
    if result.rowcount > 0:
        message_response = {
            **base_message_response,
            "status": "resolved",
            "resolution": resolution or None,
            "resolved_at": resolved_at.isoformat(),
        }
        # Persist the resolution FIRST (together with any staged workbench
        # update) so a secondary failure while posting the answer/ack reply can
        # never roll back the primary resolution — the message stays resolved
        # even if the reply cannot be posted (restores main's resilient order).
        await session.commit()
        reply_msg: ProjectMessage | None = None
        reply_message_event: dict[str, Any] | None = None
        reply_message_id: str | None = None
        reply_message_response: dict[str, Any] | None = None
        arming: str | None = None
        if resolution and message_type in (
            MessageType.QUESTION,
            MessageType.FEEDBACK,
        ):
            try:
                from app.notification_dispatch import (
                    post_feedback_acknowledgment as _post_feedback_ack,
                    post_question_answer as _post_qa,
                )

                strategic_session = await build_message_strategic_session_snapshot(
                    session,
                    current_user["user_id"],
                    message_project_uuid,
                    workbench_context_for_snapshot,
                )
                if message_type == MessageType.QUESTION:
                    reply_msg = await _post_qa(
                        session,
                        message_project_uuid,
                        message_id,
                        resolution,
                        from_type="user",
                    )
                else:
                    reply_msg = await _post_feedback_ack(
                        session,
                        message_project_uuid,
                        message_id,
                        resolution,
                    )
                if strategic_session:
                    reply_msg.context = {
                        **(reply_msg.context or {}),
                        "strategic_session": strategic_session,
                    }
                await session.commit()
                await session.refresh(reply_msg)
                reply_message_id = str(reply_msg.id)
                reply_message_event = message_event_payload(reply_msg)
                reply_message_response = admin_message_dict(reply_msg)
                event_type = (
                    reply_msg.context.get("event_type")
                    if isinstance(reply_msg.context, dict)
                    else None
                )
                # Suppress cadence steering when a live resident Workday session
                # owns the project — the standing agent replies live, so waking a
                # competing cadence writer (and posting a steer receipt) is wrong.
                from app.models import Project as _Project
                from app.strategic_resident_session import (
                    read_resident_session,
                    resident_session_is_active,
                )

                _reply_project = await session.get(_Project, message_project_uuid)
                _resident_active = _reply_project is not None and resident_session_is_active(
                    read_resident_session(_reply_project.vision)
                )
                arming = await maybe_arm_operator_refresh(
                    session,
                    message_project_uuid,
                    MessageType(reply_msg.message_type),
                    event_type=str(event_type) if event_type else None,
                    message_content=resolution,
                    resident_session_active=_resident_active,
                )
                if arming == ARMING_STEERING:
                    await session.commit()
            except Exception:
                # A reply-post/arming failure is non-fatal: the resolution is
                # already committed above and must stand. Log, discard the
                # failed secondary work, and return the resolved message alone.
                logger.warning(
                    "resolve_message reply post failed for message %s; "
                    "resolution stands",
                    message_id_str,
                    exc_info=True,
                )
                await session.rollback()
                reply_msg = None
                reply_message_event = None
                reply_message_id = None
                reply_message_response = None
                arming = None
        await broadcast_to_project(str(message_project_uuid), "message_updated", message_event)
        if reply_message_event is not None:
            await broadcast_to_project(
                str(message_project_uuid),
                "message_created",
                reply_message_event,
            )
        response = {"id": message_id_str, "status": "resolved", "message": message_response}
        if reply_message_id is not None:
            response["reply_message_id"] = reply_message_id
        if reply_message_response is not None:
            response["reply_message"] = reply_message_response
        if arming is not None:
            response["arming"] = arming
        return response
    else:
        await session.refresh(msg)
        current_message_response = admin_message_dict(msg)
        await session.commit()
        # Message was already resolved by a concurrent request
        return {"id": message_id_str, "status": "already_resolved", "message": current_message_response}


@router.delete("/messages/{message_id}")
async def delete_message(
    message_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Delete a message (idempotent)."""
    msg = await session.get(ProjectMessage, message_id)
    message_response: dict[str, Any] | None = None
    if msg:
        # Verify user has write access to the project (member+; viewers are read-only)
        await _get_user_project_with_role(
            msg.project_id, current_user["user_id"], session, "member"
        )
        message_response = {**admin_message_dict(msg), "status": "deleted"}
        await session.delete(msg)
        await session.commit()
    # Return success whether or not the message existed (idempotent)
    response: dict[str, Any] = {
        "id": str(message_id),
        "status": "deleted" if message_response is not None else "not_found",
        "deleted": True,
    }
    if message_response is not None:
        response["message"] = message_response
    return response


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
        return {"last_seen_at": naive_utc_to_epoch_ms(max_ts)}

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
    return {"last_seen_at": naive_utc_to_epoch_ms(row.last_seen_at)}


@router.put("/messages/last-seen")
async def put_messages_last_seen(
    payload: _LastSeenUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Upsert the operator's last-seen timestamp (ms epoch) for the messages panel divider."""
    try:
        pid = uuid.UUID(payload.project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    await _get_user_project(pid, current_user["user_id"], session)

    new_ts = timestamp_ms_to_naive_utc(payload.timestamp)
    stored_ts = await upsert_messages_last_seen(
        session,
        current_user["user_id"],
        pid,
        new_ts,
    )
    await session.commit()
    return {"status": "ok", "last_seen_at": naive_utc_to_epoch_ms(stored_ts)}


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

    row = await get_messages_noise_prefs_row(
        session,
        current_user["user_id"],
        pid,
    )

    prefs = dict(row.prefs or {}) if row is not None else {}
    return {"prefs": {k: v for k, v in prefs.items() if k not in _RESERVED_PREFS_KEYS}}


@router.put("/messages/noise-prefs")
async def put_messages_noise_prefs(
    payload: _NoisePrefUpdate,
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

    # This endpoint only owns event-type visibility prefs. Strip any reserved
    # keys from the incoming payload (they are owned by the temp-mutes and
    # mute-window endpoints) and preserve the existing reserved values so a
    # visibility save never clobbers an operator's active DND window or mutes.
    visibility = {
        k: v for k, v in payload.prefs.items() if k not in _RESERVED_PREFS_KEYS
    }

    def merge(existing: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(visibility)
        for key in _RESERVED_PREFS_KEYS:
            if key in existing:
                merged[key] = existing[key]
        return merged

    await mutate_messages_noise_prefs(
        session,
        current_user["user_id"],
        pid,
        merge,
    )
    await session.commit()
    return {"status": "ok"}


# ── Strategic Workbench State Endpoints ──────────────────────────────────────


@router.get("/messages/workbench-state")
async def get_messages_workbench_state(
    project_id: str = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return this operator's small Strategic workbench state for a project."""
    try:
        pid = uuid.UUID(project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    await _get_user_project(pid, current_user["user_id"], session)
    return {
        "workbench": await get_strategic_workbench_state(
            session, current_user["user_id"], pid
        )
    }


@router.put("/messages/workbench-state")
async def put_messages_workbench_state(
    payload: _WorkbenchStateUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Persist this operator's small Strategic workbench state for a project."""
    try:
        pid = uuid.UUID(payload.project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    # NOT per-user personal UI state, despite living under /messages/* alongside
    # last-seen / noise-prefs / saved-views / temp-mutes / mute-window (which ARE
    # personal — each is keyed by a UniqueConstraint("user_id","project_id") and
    # stays viewer-writable). persist_strategic_workbench_update takes
    # with_vision_locked() to read-modify-write the PROJECT-GLOBAL Project.vision
    # column, and on a client-supplied interaction_mode transition arms a sticky,
    # breaker-bypassing, cooldown-bypassing strategic refresh with
    # origin=ORIGIN_OPERATOR, persisting an operator-supplied steering excerpt.
    # Without the member+ floor a viewer can steer the autonomous strategist.
    await _get_user_project_with_role(pid, current_user["user_id"], session, "member")

    if workbench_state_size_bytes(payload.workbench) > WORKBENCH_STATE_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail="Workbench state too large (max 50KB)",
        )

    result = await persist_strategic_workbench_update(
        session,
        current_user["user_id"],
        pid,
        payload.workbench,
    )
    await emit_workbench_refetch(
        pid,
        resource="strategic_workbench",
        action="updated",
        refetch_surfaces=(SURFACE_STRATEGIC_SESSION, SURFACE_RUNTIME),
        summary_key="workbench",
        summary={
            "interaction_mode": result.state.get("interaction_mode"),
            "active_surface": result.state.get("active_surface"),
            "arming": result.arming,
        },
    )
    return {"status": "ok", "workbench": result.state, "arming": result.arming}


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

    return {"saved_views": _sanitize_message_saved_views(row.saved_views if row is not None else [])}


@router.put("/messages/saved-views")
async def put_messages_saved_views(
    payload: _SavedViewsUpdate,
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

    saved_views = _sanitize_message_saved_views(payload.saved_views)

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
            saved_views=saved_views,
        )
    else:
        row.saved_views = saved_views
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

    row = await get_messages_noise_prefs_row(
        session,
        current_user["user_id"],
        pid,
    )

    raw: Dict[str, int] = (row.prefs.get(_TEMP_MUTES_KEY) or {}) if row else {}
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    return {"temp_mutes": {et: exp for et, exp in raw.items() if exp > now_ms}}


@router.put("/messages/temp-mutes")
async def put_messages_temp_mutes(
    payload: _TempMutesUpdate,
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

    def merge(existing: Dict[str, Any]) -> Dict[str, Any]:
        prefs = dict(existing)
        prefs[_TEMP_MUTES_KEY] = active
        return prefs

    await mutate_messages_noise_prefs(
        session,
        current_user["user_id"],
        pid,
        merge,
    )
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

    row = await get_messages_noise_prefs_row(
        session,
        current_user["user_id"],
        pid,
    )

    stored = (row.prefs.get(_MUTE_WINDOW_KEY)) if row else None
    if not stored or not isinstance(stored, dict):
        return {"mute_window": None}
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    if stored.get("expires_at", 0) <= now_ms:
        return {"mute_window": None}
    return {"mute_window": stored}


@router.put("/messages/mute-window")
async def put_messages_mute_window(
    payload: _MuteWindowUpdate,
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

    def merge(existing: Dict[str, Any]) -> Dict[str, Any]:
        prefs = dict(existing)
        prefs[_MUTE_WINDOW_KEY] = entry
        return prefs

    await mutate_messages_noise_prefs(
        session,
        current_user["user_id"],
        pid,
        merge,
    )
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
    return {"last_visit_at": naive_utc_to_epoch_ms(max_ts)}


@router.put("/last-visit-timestamp")
async def put_last_visit_timestamp(
    payload: _LastSeenUpdate,
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
    new_ts = timestamp_ms_to_naive_utc(payload.timestamp)
    stored_ts = await upsert_messages_last_seen(
        session,
        current_user["user_id"],
        pid,
        new_ts,
    )
    await session.commit()
    return {"status": "ok", "last_visit_at": naive_utc_to_epoch_ms(stored_ts)}


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
    payload: _AdminPlaybookSet,
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
