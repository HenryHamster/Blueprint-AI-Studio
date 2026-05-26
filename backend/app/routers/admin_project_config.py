"""Admin project-level configuration endpoints.

Per-project configuration for auto-remediation, issue-implementation, and
auto-review pipelines, plus the cross-project cost-summary analytics route.
Each config endpoint pair (GET/PATCH) reads and merges stored project config
with module-level defaults, then persists partial updates back into
``Project.design_doc``.
"""

import re
import uuid
import logging
from typing import Dict, Any
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, case
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm import load_only
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import get_session
from app.models import Task, TaskStatus, _utcnow
from app.auth import get_current_user, require_admin
from app.admin import _get_user_project_with_role
from app.task_locking import lock_project_or_429, with_design_doc_locked
from app.playbook_storage import archive_playbook_entry
from app.shared_state import (
    get_project_integration_branch,
    get_project_promotion_target,
    get_project_task_branch_namespace,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-project-config"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/cost-summary")
async def cost_summary(
    session: AsyncSession = Depends(get_session),
    days: int = Query(7, ge=1, le=365),
    current_user: dict = Depends(require_admin),
):
    # e2-standard-2 pricing: ~$0.067/hr on-demand, ~$0.020/hr spot
    ON_DEMAND_RATE = 0.067 / 3600  # per second
    SPOT_RATE = 0.020 / 3600

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    duration_seconds = func.extract(
        "epoch", Task.vm_stopped_at - Task.vm_provisioned_at
    )
    rate_expr = case((Task.vm_spot, SPOT_RATE), else_=ON_DEMAND_RATE)

    rows = (
        await session.execute(
            select(
                Task.provider,
                Task.vm_spot,
                func.count().label("count"),
                func.sum(duration_seconds).label("total_seconds"),
                func.sum(duration_seconds * rate_expr).label("estimated_cost"),
            )
            .where(
                Task.created_at >= cutoff,
                Task.vm_provisioned_at.is_not(None),
                Task.vm_stopped_at.is_not(None),
            )
            .group_by(Task.provider, Task.vm_spot)
        )
    ).all()

    by_type: Dict[str, Any] = {}
    total_cost = 0.0
    for row in rows:
        key = f"{row.provider or 'unknown'}:{'spot' if row.vm_spot else 'on-demand'}"
        estimated_cost = float(row.estimated_cost or 0.0)
        by_type[key] = {
            "count": row.count,
            "total_seconds": float(row.total_seconds or 0.0),
            "estimated_cost": estimated_cost,
            "provider": row.provider,
            "spot": row.vm_spot,
        }
        total_cost += estimated_cost

    return {
        "period_days": days,
        "by_task_type": by_type,
        "total_estimated_cost": total_cost,
    }


# ---------------------------------------------------------------------------
# Auto-remediation config endpoints
# ---------------------------------------------------------------------------

_AUTO_REMEDIATION_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "branch_whitelist": [],
    "max_per_hour": 2,
    "max_concurrent": 2,
    "require_review": True,
    "suspended_branches": [],
}


def _build_auto_remediation_defaults(project) -> Dict[str, Any]:
    integration = get_project_integration_branch(project)
    promotion = get_project_promotion_target(project)
    branch_whitelist = list(dict.fromkeys([integration, promotion]))
    return {**_AUTO_REMEDIATION_DEFAULTS, "branch_whitelist": branch_whitelist}


@router.get("/projects/{project_id}/auto-remediation")
async def get_auto_remediation_config(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the current auto-remediation config and activity stats for a project."""
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    stored = (project.design_doc or {}).get("_auto_remediation", {})
    config = {**_build_auto_remediation_defaults(project), **stored}

    # Compute activity stats from tasks spawned by auto-remediation
    now = _utcnow()
    seven_days_ago = now - timedelta(days=7)
    twenty_four_hours_ago = now - timedelta(hours=24)

    recent_rows = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.created_at >= seven_days_ago,
                )
                .options(
                    load_only(
                        Task.id, Task.status, Task.title, Task.context, Task.created_at
                    )
                )
                .order_by(Task.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    auto_tasks = [
        t for t in recent_rows if (t.context or {}).get("auto_remediation") is True
    ]
    spawned_24h = sum(
        1 for t in auto_tasks if t.created_at and t.created_at >= twenty_four_hours_ago
    )
    spawned_7d = len(auto_tasks)

    terminal = [
        t for t in auto_tasks if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
    ]
    success_rate = (
        round(
            sum(1 for t in terminal if t.status == TaskStatus.COMPLETED)
            / len(terminal)
            * 100
        )
        if terminal
        else None
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
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    # Validate incoming fields
    allowed = set(_AUTO_REMEDIATION_DEFAULTS.keys())
    unknown = set(body.keys()) - allowed
    if unknown:
        raise HTTPException(
            status_code=422, detail=f"Unknown config fields: {sorted(unknown)}"
        )

    if "enabled" in body and not isinstance(body["enabled"], bool):
        raise HTTPException(status_code=422, detail="'enabled' must be a boolean")

    if "branch_whitelist" in body:
        bw = body["branch_whitelist"]
        if not isinstance(bw, list) or not all(isinstance(b, str) and b for b in bw):
            raise HTTPException(
                status_code=422,
                detail="'branch_whitelist' must be a list of non-empty strings",
            )

    if "max_per_hour" in body:
        v = body["max_per_hour"]
        if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= 10):
            raise HTTPException(
                status_code=422,
                detail="'max_per_hour' must be an integer between 1 and 10",
            )

    if "max_concurrent" in body:
        v = body["max_concurrent"]
        if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= 5):
            raise HTTPException(
                status_code=422,
                detail="'max_concurrent' must be an integer between 1 and 5",
            )

    if "require_review" in body and not isinstance(body["require_review"], bool):
        raise HTTPException(
            status_code=422, detail="'require_review' must be a boolean"
        )

    if "suspended_branches" in body:
        sb = body["suspended_branches"]
        if not isinstance(sb, list) or not all(isinstance(b, str) for b in sb):
            raise HTTPException(
                status_code=422, detail="'suspended_branches' must be a list of strings"
            )

    # Lock project row before read-modify-write to prevent lost updates.
    project = await lock_project_or_429(session, project_id)
    design_doc = dict(project.design_doc or {})
    stored = dict(design_doc.get("_auto_remediation", {}))
    stored.update(body)
    design_doc["_auto_remediation"] = stored
    project.design_doc = design_doc
    flag_modified(project, "design_doc")
    session.add(project)
    await session.commit()

    config = {**_build_auto_remediation_defaults(project), **stored}
    return {"project_id": str(project_id), "config": config}


# ---------------------------------------------------------------------------
# Issue-to-Implementation Config
# ---------------------------------------------------------------------------

_ISSUE_IMPLEMENTATION_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "trigger_label": "auto:implement",
    "max_per_hour": 3,
    "max_concurrent": 3,
    "default_effort": "high",
}


def _build_issue_implementation_defaults(project) -> Dict[str, Any]:
    return {
        **_ISSUE_IMPLEMENTATION_DEFAULTS,
        "base_branch": get_project_integration_branch(project),
    }


@router.get("/projects/{project_id}/issue-implementation")
async def get_issue_implementation_config(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the current issue-implementation config for a project, merged with defaults."""
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    stored = {
        k: v
        for k, v in (project.design_doc or {}).get("_issue_implementation", {}).items()
        if k != "base_branch"
    }
    config = {**_build_issue_implementation_defaults(project), **stored}
    return {"project_id": str(project_id), "config": config}


@router.patch("/projects/{project_id}/issue-implementation")
async def update_issue_implementation_config(
    project_id: uuid.UUID,
    body: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Update issue-implementation config fields (partial update)."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    # Validate incoming fields
    allowed = set(_ISSUE_IMPLEMENTATION_DEFAULTS.keys())
    unknown = set(body.keys()) - allowed
    if unknown:
        raise HTTPException(
            status_code=422, detail=f"Unknown config fields: {sorted(unknown)}"
        )

    if "enabled" in body and not isinstance(body["enabled"], bool):
        raise HTTPException(status_code=422, detail="'enabled' must be a boolean")

    if "trigger_label" in body:
        v = body["trigger_label"]
        if not isinstance(v, str) or not v.strip():
            raise HTTPException(
                status_code=422, detail="'trigger_label' must be a non-empty string"
            )

    if "max_per_hour" in body:
        v = body["max_per_hour"]
        if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= 10):
            raise HTTPException(
                status_code=422,
                detail="'max_per_hour' must be an integer between 1 and 10",
            )

    if "max_concurrent" in body:
        v = body["max_concurrent"]
        if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= 5):
            raise HTTPException(
                status_code=422,
                detail="'max_concurrent' must be an integer between 1 and 5",
            )

    if "default_effort" in body:
        v = body["default_effort"]
        if not isinstance(v, str) or v not in ("low", "medium", "high"):
            raise HTTPException(
                status_code=422,
                detail="'default_effort' must be one of: low, medium, high",
            )

    # Lock project row before read-modify-write to prevent lost updates.
    project = await lock_project_or_429(session, project_id)
    design_doc = dict(project.design_doc or {})
    stored = {
        k: v
        for k, v in design_doc.get("_issue_implementation", {}).items()
        if k != "base_branch"
    }
    stored.update(body)
    design_doc["_issue_implementation"] = stored
    project.design_doc = design_doc
    flag_modified(project, "design_doc")
    session.add(project)
    await session.commit()

    config = {**_build_issue_implementation_defaults(project), **stored}
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
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
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
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    # Validate incoming fields
    allowed = set(_AUTO_REVIEW_DEFAULTS.keys())
    unknown = set(body.keys()) - allowed
    if unknown:
        raise HTTPException(
            status_code=422, detail=f"Unknown config fields: {sorted(unknown)}"
        )

    if "enabled" in body and not isinstance(body["enabled"], bool):
        raise HTTPException(status_code=422, detail="'enabled' must be a boolean")

    if "max_per_hour" in body:
        v = body["max_per_hour"]
        if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= 20):
            raise HTTPException(
                status_code=422,
                detail="'max_per_hour' must be an integer between 1 and 20",
            )

    if "max_concurrent" in body:
        v = body["max_concurrent"]
        if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= 10):
            raise HTTPException(
                status_code=422,
                detail="'max_concurrent' must be an integer between 1 and 10",
            )

    # Lock project row before read-modify-write to prevent lost updates.
    project = await lock_project_or_429(session, project_id)
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
# Integration-branch config endpoints
# ---------------------------------------------------------------------------

_VALID_BRANCH_RE = re.compile(r"^[a-zA-Z0-9._/\-]+$")


@router.get("/projects/{project_id}/integration-branch")
async def get_integration_branch_config(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the per-project integration branch (trunk for autonomous spawns)."""
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    return {
        "project_id": str(project_id),
        "integration_branch": get_project_integration_branch(project),
        "is_default": project.integration_branch is None,
    }


@router.patch("/projects/{project_id}/integration-branch")
async def update_integration_branch_config(
    project_id: uuid.UUID,
    body: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Set the per-project integration branch. Pass null to revert to global default."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    if "integration_branch" not in body:
        raise HTTPException(
            status_code=422, detail="Request body must include 'integration_branch'"
        )

    branch = body["integration_branch"]
    if branch is not None:
        if not isinstance(branch, str) or not branch.strip():
            raise HTTPException(
                status_code=422,
                detail="'integration_branch' must be a non-empty string or null",
            )
        if not _VALID_BRANCH_RE.match(branch):
            raise HTTPException(
                status_code=422,
                detail="'integration_branch' contains invalid characters",
            )

    project = await lock_project_or_429(session, project_id)
    project.integration_branch = branch
    session.add(project)
    await session.commit()

    return {
        "project_id": str(project_id),
        "integration_branch": get_project_integration_branch(project),
        "is_default": project.integration_branch is None,
    }


# ---------------------------------------------------------------------------
# Promotion-target config endpoints
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/promotion-target")
async def get_promotion_target_config(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the per-project promotion/LTS target branch."""
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    return {
        "project_id": str(project_id),
        "promotion_target": get_project_promotion_target(project),
        "is_default": project.promotion_target is None,
    }


@router.patch("/projects/{project_id}/promotion-target")
async def update_promotion_target_config(
    project_id: uuid.UUID,
    body: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Set the per-project promotion/LTS target branch. Pass null to revert to global default."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    if "promotion_target" not in body:
        raise HTTPException(
            status_code=422, detail="Request body must include 'promotion_target'"
        )

    branch = body["promotion_target"]
    if branch is not None:
        if not isinstance(branch, str) or not branch.strip():
            raise HTTPException(
                status_code=422,
                detail="'promotion_target' must be a non-empty string or null",
            )
        if not _VALID_BRANCH_RE.match(branch):
            raise HTTPException(
                status_code=422,
                detail="'promotion_target' contains invalid characters",
            )

    project = await lock_project_or_429(session, project_id)
    project.promotion_target = branch
    session.add(project)
    await session.commit()

    return {
        "project_id": str(project_id),
        "promotion_target": get_project_promotion_target(project),
        "is_default": project.promotion_target is None,
    }


# ---------------------------------------------------------------------------
# Task-branch namespace / prefix config endpoints
# ---------------------------------------------------------------------------


def _build_config_has_key(project, key: str) -> bool:
    value = (project.build_config or {}).get(key)
    return value not in (None, "")


@router.get("/projects/{project_id}/task-branch-namespace")
async def get_task_branch_namespace_config(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the per-project task-branch namespace (leading segment of agent task branches)."""
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    return {
        "project_id": str(project_id),
        "task_branch_namespace": get_project_task_branch_namespace(project),
        "is_default": not _build_config_has_key(project, "task_branch_namespace"),
    }


@router.patch("/projects/{project_id}/task-branch-namespace")
async def update_task_branch_namespace_config(
    project_id: uuid.UUID,
    body: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Set the per-project task-branch namespace. Pass null to revert to the global default."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    if "task_branch_namespace" not in body:
        raise HTTPException(
            status_code=422,
            detail="Request body must include 'task_branch_namespace'",
        )

    namespace = body["task_branch_namespace"]
    if namespace is not None:
        if not isinstance(namespace, str) or not namespace.strip():
            raise HTTPException(
                status_code=422,
                detail="'task_branch_namespace' must be a non-empty string or null",
            )
        if not _VALID_BRANCH_RE.match(namespace):
            raise HTTPException(
                status_code=422,
                detail="'task_branch_namespace' contains invalid characters",
            )
        if "/" in namespace:
            raise HTTPException(
                status_code=422,
                detail="'task_branch_namespace' must be a single segment (no '/')",
            )

    project = await lock_project_or_429(session, project_id)
    build_config = dict(project.build_config or {})
    if namespace is None:
        build_config.pop("task_branch_namespace", None)
    else:
        build_config["task_branch_namespace"] = namespace
    project.build_config = build_config
    flag_modified(project, "build_config")
    session.add(project)
    await session.commit()

    return {
        "project_id": str(project_id),
        "task_branch_namespace": get_project_task_branch_namespace(project),
        "is_default": not _build_config_has_key(project, "task_branch_namespace"),
    }


@router.get("/projects/{project_id}/task-branch-prefix")
async def get_task_branch_prefix_config(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the per-project task-branch prefix (defaults to ``{namespace}/`` when unset)."""
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    return {
        "project_id": str(project_id),
        "task_branch_prefix": project.task_branch_prefix,
        "is_default": not _build_config_has_key(project, "task_branch_prefix"),
    }


@router.patch("/projects/{project_id}/task-branch-prefix")
async def update_task_branch_prefix_config(
    project_id: uuid.UUID,
    body: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Set the per-project task-branch prefix. Pass null to revert to the derived default."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    if "task_branch_prefix" not in body:
        raise HTTPException(
            status_code=422,
            detail="Request body must include 'task_branch_prefix'",
        )

    prefix = body["task_branch_prefix"]
    if prefix is not None:
        if not isinstance(prefix, str) or not prefix.strip():
            raise HTTPException(
                status_code=422,
                detail="'task_branch_prefix' must be a non-empty string or null",
            )
        if not _VALID_BRANCH_RE.match(prefix):
            raise HTTPException(
                status_code=422,
                detail="'task_branch_prefix' contains invalid characters",
            )

    project = await lock_project_or_429(session, project_id)
    build_config = dict(project.build_config or {})
    if prefix is None:
        build_config.pop("task_branch_prefix", None)
    else:
        build_config["task_branch_prefix"] = prefix
    project.build_config = build_config
    flag_modified(project, "build_config")
    session.add(project)
    await session.commit()

    return {
        "project_id": str(project_id),
        "task_branch_prefix": project.task_branch_prefix,
        "is_default": not _build_config_has_key(project, "task_branch_prefix"),
    }


# ---------------------------------------------------------------------------
# Broadcast-limit config endpoints
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/broadcast-limit")
async def get_broadcast_limit_config(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the per-project broadcast limit (None means use the global default)."""
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    stored = (project.design_doc or {}).get("_broadcast_limit")
    return {
        "project_id": str(project_id),
        "broadcast_limit": stored,
        "is_default": stored is None,
    }


@router.patch("/projects/{project_id}/broadcast-limit")
async def update_broadcast_limit_config(
    project_id: uuid.UUID,
    body: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Set the per-project broadcast limit. Pass null to revert to the global default."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    if "broadcast_limit" not in body:
        raise HTTPException(
            status_code=422, detail="Request body must include 'broadcast_limit'"
        )

    value = body["broadcast_limit"]
    if value is not None:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise HTTPException(
                status_code=422,
                detail="'broadcast_limit' must be a non-negative integer or null",
            )

    def _apply(doc: dict) -> None:
        if value is None:
            doc.pop("_broadcast_limit", None)
        else:
            doc["_broadcast_limit"] = value

    await with_design_doc_locked(session, project_id, _apply)
    await session.commit()

    return {
        "project_id": str(project_id),
        "broadcast_limit": value,
        "is_default": value is None,
    }


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
        raise HTTPException(
            status_code=422, detail="'action' must be one of: promote, demote, archive"
        )

    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    # Lock project row before read-modify-write to prevent lost updates.
    project = await lock_project_or_429(session, project_id)
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
        archive_playbook_entry(doc, full_key, archived_at=now_iso)

    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)
    await session.commit()

    if action == "archive":
        return {
            "project_id": str(project_id),
            "key": key,
            "action": action,
            "archived": True,
        }
    confirmed = entry.get("confirmed_count", 0)
    referenced = entry.get("reference_count", 0)
    return {
        "project_id": str(project_id),
        "key": key,
        "action": action,
        "entry": {
            "confirmed_count": confirmed,
            "reference_count": referenced,
            "stale": bool(entry.get("stale")),
            "effectiveness_score": min(round(confirmed / max(referenced, 1), 3), 1.0),
        },
    }
