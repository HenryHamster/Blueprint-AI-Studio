"""Admin project-level configuration endpoints.

Per-project configuration for auto-remediation, issue-implementation, and
auto-review pipelines, plus the cross-project cost-summary analytics route.
Each config endpoint pair (GET/PATCH) reads and merges stored project config
with module-level defaults, then persists partial updates back into
``Project.design_doc``.
"""
import uuid
import logging
from typing import Dict, Any
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm import load_only
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import get_session
from app.models import Task, TaskStatus, _utcnow
from app.auth import get_current_user, require_admin
from app.admin import _get_user_project_with_role

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-project-config"],
    dependencies=[Depends(get_current_user)]
)


@router.get('/cost-summary')
async def cost_summary(
    session: AsyncSession = Depends(get_session),
    days: int = Query(7, ge=1, le=365),
    current_user: dict = Depends(require_admin),
):
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    tasks = (await session.execute(
        select(Task).where(Task.created_at >= cutoff)
    )).scalars().all()

    # e2-standard-2 pricing: ~$0.067/hr on-demand, ~$0.020/hr spot
    ON_DEMAND_RATE = 0.067 / 3600  # per second
    SPOT_RATE = 0.020 / 3600

    by_type = defaultdict(lambda: {'count': 0, 'total_seconds': 0, 'estimated_cost': 0.0})
    for t in tasks:
        if t.vm_provisioned_at and t.vm_stopped_at:
            seconds = (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
            rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
            task_type = t.first_payload.get('type', 'unknown')
            by_type[task_type]['count'] += 1
            by_type[task_type]['total_seconds'] += seconds
            by_type[task_type]['estimated_cost'] += seconds * rate

    return {
        'period_days': days,
        'by_task_type': dict(by_type),
        'total_estimated_cost': sum(v['estimated_cost'] for v in by_type.values()),
    }


# ---------------------------------------------------------------------------
# Auto-remediation config endpoints
# ---------------------------------------------------------------------------

_AUTO_REMEDIATION_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "branch_whitelist": ["main", "master"],
    "max_per_hour": 2,
    "max_concurrent": 2,
    "require_review": True,
    "suspended_branches": [],
}


@router.get("/projects/{project_id}/auto-remediation")
async def get_auto_remediation_config(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the current auto-remediation config and activity stats for a project."""
    project = await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")
    stored = (project.design_doc or {}).get("_auto_remediation", {})
    config = {**_AUTO_REMEDIATION_DEFAULTS, **stored}

    # Compute activity stats from tasks spawned by auto-remediation
    now = _utcnow()
    seven_days_ago = now - timedelta(days=7)
    twenty_four_hours_ago = now - timedelta(hours=24)

    recent_rows = (await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.created_at >= seven_days_ago,
        ).options(
            load_only(Task.id, Task.status, Task.title, Task.context, Task.created_at)
        ).order_by(Task.created_at.desc())
    )).scalars().all()

    auto_tasks = [t for t in recent_rows if (t.context or {}).get("auto_remediation") is True]
    spawned_24h = sum(1 for t in auto_tasks if t.created_at and t.created_at >= twenty_four_hours_ago)
    spawned_7d = len(auto_tasks)

    terminal = [t for t in auto_tasks if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)]
    success_rate = (
        round(sum(1 for t in terminal if t.status == TaskStatus.COMPLETED) / len(terminal) * 100)
        if terminal else None
    )

    recent_tasks_data = [
        {
            "id": str(t.id),
            "title": t.title or "",
            "status": t.status,
            "branch": (t.context or {}).get("ci_failure_branch", ""),
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in auto_tasks[:5]
    ]

    return {
        "project_id": str(project_id),
        "config": config,
        "stats": {
            "spawned_24h": spawned_24h,
            "spawned_7d": spawned_7d,
            "success_rate": success_rate,
            "recent_tasks": recent_tasks_data,
        },
    }


@router.patch("/projects/{project_id}/auto-remediation")
async def update_auto_remediation_config(
    project_id: uuid.UUID,
    body: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Update auto-remediation config fields (partial update)."""
    project = await _get_user_project_with_role(project_id, current_user["user_id"], session, "owner")

    # Validate incoming fields
    allowed = set(_AUTO_REMEDIATION_DEFAULTS.keys())
    unknown = set(body.keys()) - allowed
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown config fields: {sorted(unknown)}")

    if "enabled" in body and not isinstance(body["enabled"], bool):
        raise HTTPException(status_code=422, detail="'enabled' must be a boolean")

    if "branch_whitelist" in body:
        bw = body["branch_whitelist"]
        if not isinstance(bw, list) or not all(isinstance(b, str) and b for b in bw):
            raise HTTPException(status_code=422, detail="'branch_whitelist' must be a list of non-empty strings")

    if "max_per_hour" in body:
        v = body["max_per_hour"]
        if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= 10):
            raise HTTPException(status_code=422, detail="'max_per_hour' must be an integer between 1 and 10")

    if "max_concurrent" in body:
        v = body["max_concurrent"]
        if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= 5):
            raise HTTPException(status_code=422, detail="'max_concurrent' must be an integer between 1 and 5")

    if "require_review" in body and not isinstance(body["require_review"], bool):
        raise HTTPException(status_code=422, detail="'require_review' must be a boolean")

    if "suspended_branches" in body:
        sb = body["suspended_branches"]
        if not isinstance(sb, list) or not all(isinstance(b, str) for b in sb):
            raise HTTPException(status_code=422, detail="'suspended_branches' must be a list of strings")

    # Merge into existing stored config
    design_doc = dict(project.design_doc or {})
    stored = dict(design_doc.get("_auto_remediation", {}))
    stored.update(body)
    design_doc["_auto_remediation"] = stored
    project.design_doc = design_doc
    flag_modified(project, "design_doc")
    session.add(project)
    await session.commit()

    config = {**_AUTO_REMEDIATION_DEFAULTS, **stored}
    return {"project_id": str(project_id), "config": config}


# ---------------------------------------------------------------------------
# Issue-to-Implementation Config
# ---------------------------------------------------------------------------

_ISSUE_IMPLEMENTATION_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "trigger_label": "blueprint:implement",
    "max_per_hour": 3,
    "max_concurrent": 3,
    "base_branch": "main",
    "default_effort": "high",
}


@router.get("/projects/{project_id}/issue-implementation")
async def get_issue_implementation_config(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the current issue-implementation config for a project, merged with defaults."""
    project = await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")
    stored = (project.design_doc or {}).get("_issue_implementation", {})
    config = {**_ISSUE_IMPLEMENTATION_DEFAULTS, **stored}
    return {"project_id": str(project_id), "config": config}


@router.patch("/projects/{project_id}/issue-implementation")
async def update_issue_implementation_config(
    project_id: uuid.UUID,
    body: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Update issue-implementation config fields (partial update)."""
    project = await _get_user_project_with_role(project_id, current_user["user_id"], session, "owner")

    # Validate incoming fields
    allowed = set(_ISSUE_IMPLEMENTATION_DEFAULTS.keys())
    unknown = set(body.keys()) - allowed
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown config fields: {sorted(unknown)}")

    if "enabled" in body and not isinstance(body["enabled"], bool):
        raise HTTPException(status_code=422, detail="'enabled' must be a boolean")

    if "trigger_label" in body:
        v = body["trigger_label"]
        if not isinstance(v, str) or not v.strip():
            raise HTTPException(status_code=422, detail="'trigger_label' must be a non-empty string")

    if "max_per_hour" in body:
        v = body["max_per_hour"]
        if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= 10):
            raise HTTPException(status_code=422, detail="'max_per_hour' must be an integer between 1 and 10")

    if "max_concurrent" in body:
        v = body["max_concurrent"]
        if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= 5):
            raise HTTPException(status_code=422, detail="'max_concurrent' must be an integer between 1 and 5")

    if "base_branch" in body:
        v = body["base_branch"]
        if not isinstance(v, str) or not v.strip():
            raise HTTPException(status_code=422, detail="'base_branch' must be a non-empty string")

    if "default_effort" in body:
        v = body["default_effort"]
        if not isinstance(v, str) or v not in ("low", "medium", "high"):
            raise HTTPException(status_code=422, detail="'default_effort' must be one of: low, medium, high")

    # Merge into existing stored config
    design_doc = dict(project.design_doc or {})
    stored = dict(design_doc.get("_issue_implementation", {}))
    stored.update(body)
    design_doc["_issue_implementation"] = stored
    project.design_doc = design_doc
    flag_modified(project, "design_doc")
    session.add(project)
    await session.commit()

    config = {**_ISSUE_IMPLEMENTATION_DEFAULTS, **stored}
    return {"project_id": str(project_id), "config": config}


# ---------------------------------------------------------------------------
# Auto-Review Config
# ---------------------------------------------------------------------------

_AUTO_REVIEW_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "max_per_hour": 5,
    "max_concurrent": 3,
}


@router.get("/projects/{project_id}/auto-review")
async def get_auto_review_config(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the current auto-review config for a project, merged with defaults."""
    project = await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")
    stored = (project.design_doc or {}).get("_auto_review", {})
    config = {**_AUTO_REVIEW_DEFAULTS, **stored}
    return {"project_id": str(project_id), "config": config}


@router.patch("/projects/{project_id}/auto-review")
async def update_auto_review_config(
    project_id: uuid.UUID,
    body: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Update auto-review config fields (partial update)."""
    project = await _get_user_project_with_role(project_id, current_user["user_id"], session, "owner")

    # Validate incoming fields
    allowed = set(_AUTO_REVIEW_DEFAULTS.keys())
    unknown = set(body.keys()) - allowed
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown config fields: {sorted(unknown)}")

    if "enabled" in body and not isinstance(body["enabled"], bool):
        raise HTTPException(status_code=422, detail="'enabled' must be a boolean")

    if "max_per_hour" in body:
        v = body["max_per_hour"]
        if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= 20):
            raise HTTPException(status_code=422, detail="'max_per_hour' must be an integer between 1 and 20")

    if "max_concurrent" in body:
        v = body["max_concurrent"]
        if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= 10):
            raise HTTPException(status_code=422, detail="'max_concurrent' must be an integer between 1 and 10")

    # Merge into existing stored config
    design_doc = dict(project.design_doc or {})
    stored = dict(design_doc.get("_auto_review", {}))
    stored.update(body)
    design_doc["_auto_review"] = stored
    project.design_doc = design_doc
    flag_modified(project, "design_doc")
    session.add(project)
    await session.commit()

    config = {**_AUTO_REVIEW_DEFAULTS, **stored}
    return {"project_id": str(project_id), "config": config}


# ---------------------------------------------------------------------------
# Playbook Inspector endpoints
# ---------------------------------------------------------------------------

@router.patch("/projects/{project_id}/playbook/{key:path}")
async def update_playbook_entry(
    project_id: uuid.UUID,
    key: str,
    body: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Apply an operation to a playbook entry: promote, demote, or archive."""
    action = body.get("action")
    if action not in ("promote", "demote", "archive"):
        raise HTTPException(status_code=422, detail="'action' must be one of: promote, demote, archive")

    project = await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")
    doc = dict(project.design_doc or {})
    full_key = f"playbook/{key}"
    if full_key not in doc or not isinstance(doc[full_key], dict):
        raise HTTPException(status_code=404, detail=f"Playbook entry '{key}' not found")

    entry = dict(doc[full_key])
    now_iso = _utcnow().isoformat()

    if action == "promote":
        entry["confirmed_count"] = entry.get("confirmed_count", 0) + 1
        outcomes = list(entry.get("recent_outcomes", []))
        outcomes.append(True)
        entry["recent_outcomes"] = outcomes[-10:]
        entry["stale"] = False
        entry["updated_at"] = now_iso
        doc[full_key] = entry
    elif action == "demote":
        entry["stale"] = True
        entry["effectiveness_score"] = 0.0
        entry["updated_at"] = now_iso
        doc[full_key] = entry
    elif action == "archive":
        victim = dict(doc.pop(full_key))
        victim["_archived_key"] = full_key
        victim["_archived_at"] = now_iso
        archive = list(doc.get("playbook_archive") or [])
        archive.append(victim)
        doc["playbook_archive"] = archive[-50:]

    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)
    await session.commit()

    if action == "archive":
        return {"project_id": str(project_id), "key": key, "action": action, "archived": True}
    confirmed = entry.get("confirmed_count", 0)
    referenced = entry.get("reference_count", 0)
    return {"project_id": str(project_id), "key": key, "action": action, "entry": {
        "confirmed_count": confirmed,
        "reference_count": referenced,
        "stale": bool(entry.get("stale")),
        "effectiveness_score": min(round(confirmed / max(referenced, 1), 3), 1.0),
    }}
