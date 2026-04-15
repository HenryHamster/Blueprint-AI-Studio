"""Alert, message, and playbook management endpoints."""
import logging
import re
import uuid
import json
from datetime import timezone
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, update
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import get_session
from app.models import Alert, ProjectMember, ProjectMessage, MessageType, Project, _utcnow
from app.auth import get_current_user
from app.shared_state import limiter
from app.admin import _get_user_project, _get_user_project_with_role, _user_project_ids

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-notifications"],
    dependencies=[Depends(get_current_user)]
)


# ── Pydantic Models ───────────────────────────────────────────────────────────

class MessageCreate(BaseModel):
    project_id: str
    message_type: str = "NOTE"
    content: str
    context: Optional[Dict[str, Any]] = None

class MessageResolve(BaseModel):
    resolution: Optional[str] = None

class AdminPlaybookSet(BaseModel):
    key: str = Field(..., description="Playbook key: alphanumeric + hyphens, max 50 chars")
    value: str = Field(..., description="Playbook value: plain text, max 2048 bytes")
    applicable_types: Optional[List[str]] = Field(None, description="Task types this entry applies to; None = shown to all types")


# ── Constants ─────────────────────────────────────────────────────────────────

_ADMIN_PLAYBOOK_KEY_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9\-]{0,49}$')

from app.payload_schemas import ALL_TASK_TYPES as _VALID_TASK_TYPES  # noqa: E402

_SEED_ENTRIES: Dict[str, dict] = {
    "build-backend": {
        "content": (
            "Run backend tests with: `bash scripts/test-backend.sh` (builds Docker, runs full suite). "
            "Specific file: `bash scripts/test-backend.sh testing/test_file.py`. "
            "Standalone (no DB): `bash scripts/test-backend.sh --standalone`. "
            "Do NOT use pytest directly."
        ),
        "applicable_types": ["create", "fix", "refactor", "qa", "migrate", "deps", "smoketest"],
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
    query = select(Alert).where(Alert.project_id.in_(_user_project_ids(current_user["user_id"]))).order_by(Alert.created_at.desc())
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
    member = (await session.execute(
        select(ProjectMember).where(ProjectMember.project_id == alert.project_id, ProjectMember.user_id == current_user["user_id"])
    )).scalar_one_or_none()
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
    stmt = update(Alert).where(Alert.dismissed_at.is_(None), Alert.project_id.in_(_user_project_ids(current_user["user_id"])))
    if project_id:
        stmt = stmt.where(Alert.project_id == project_id)
    stmt = stmt.values(dismissed_at=_utcnow())
    result = await session.execute(stmt)
    await session.commit()
    return {"dismissed_count": result.rowcount}


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
            raise HTTPException(status_code=400, detail="Invalid status filter. Must be 'open', 'resolved', or 'all'")
        filters.append(ProjectMessage.status == status)

    total = (await session.execute(
        select(func.count()).select_from(ProjectMessage).where(*filters)
    )).scalar() or 0

    result = await session.execute(
        select(ProjectMessage).where(*filters)
        .order_by(ProjectMessage.created_at.desc())
        .offset(offset).limit(limit)
    )
    messages = result.scalars().all()

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
    # Verify user has access
    await _get_user_project(project_id, current_user["user_id"], session)

    valid_types = {t.value for t in MessageType}
    msg_type = payload.message_type.upper()
    if msg_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"Invalid message_type. Must be one of: {', '.join(valid_types)}")
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="Content cannot be empty")
    # Cap context size to prevent storage abuse
    ctx = payload.context or {}
    if len(json.dumps(ctx)) > 50_000:
        raise HTTPException(status_code=400, detail="Context too large (max 50KB)")

    msg = ProjectMessage(
        project_id=project_id,
        message_type=MessageType(msg_type),
        from_type="user",
        content=payload.content.strip()[:5000],
        context=ctx,
    )
    session.add(msg)
    await session.commit()
    await session.refresh(msg)
    return {"id": str(msg.id), "status": "created"}

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
    msg.status = "resolved"
    msg.resolution = (payload.resolution or "").strip()[:5000] or None
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
    project = await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")
    doc = project.design_doc or {}
    entries = {}
    for k, v in doc.items():
        if k.startswith("playbook/") and isinstance(v, dict):
            short_key = k[len("playbook/"):]
            last_referenced_at = v.get("last_referenced_at")
            updated_at = v.get("updated_at", "")
            # Compute stale: True if no reference in 30 days (or never referenced and updated 30+ days ago)
            now_utc = _utcnow()
            _STALE_DAYS = 30
            stale = False
            reference_ts = last_referenced_at or updated_at
            if reference_ts:
                try:
                    from datetime import datetime
                    ts = datetime.fromisoformat(reference_ts)
                    if ts.tzinfo is not None:
                        ts = ts.replace(tzinfo=None)
                    stale = (now_utc - ts).days >= _STALE_DAYS
                except Exception:
                    logger.exception(f"[PLAYBOOK] Failed to parse reference_ts={reference_ts}")
            else:
                stale = True
            entries[short_key] = {
                "content": v.get("content", ""),
                "source_task_id": v.get("source_task_id"),
                "updated_at": updated_at,
                "confirmed_count": v.get("confirmed_count", 0),
                "applicable_types": v.get("applicable_types"),
                "reference_count": v.get("reference_count", 0),
                "last_referenced_at": last_referenced_at,
                "stale": stale,
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
    await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")

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

    result = await session.execute(
        select(Project).where(Project.id == project_id).with_for_update()
    )
    project = result.scalars().first()

    doc = dict(project.design_doc or {})
    playbook_key = f"playbook/{key}"
    existing = doc.get(playbook_key, {})
    new_entry: dict = {
        "content": payload.value,
        "source_task_id": None,
        "updated_at": _utcnow().isoformat(),
        "confirmed_count": existing.get("confirmed_count", 0) if isinstance(existing, dict) else 0,
    }
    if payload.applicable_types is not None:
        new_entry["applicable_types"] = payload.applicable_types
    doc[playbook_key] = new_entry

    playbook_total = sum(
        len(json.dumps(v)) for k, v in doc.items() if k.startswith("playbook/")
    )
    if playbook_total > 30_000:
        raise HTTPException(
            status_code=400,
            detail=f"Playbook too large ({playbook_total} bytes, max 30KB total)",
        )

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
    await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")

    result = await session.execute(
        select(Project).where(Project.id == project_id).with_for_update()
    )
    project = result.scalars().first()

    doc = dict(project.design_doc or {})
    playbook_key = f"playbook/{key}"
    if playbook_key not in doc:
        raise HTTPException(status_code=404, detail=f"Playbook key '{key}' not found")

    del doc[playbook_key]

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
    await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")

    result = await session.execute(
        select(Project).where(Project.id == project_id).with_for_update()
    )
    project = result.scalars().first()

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
