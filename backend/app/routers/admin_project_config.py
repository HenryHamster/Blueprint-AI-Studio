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
from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm import load_only
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from pydantic import ValidationError

from app.database import get_session
from app.models import (
    DeploymentState,
    Task,
    TaskStatus,
    _utcnow,
)
from app.vm_cost_rates import (
    get_cloud_run_rate_for_machine_type,
    get_vm_rate_per_second,
)
from app.auth import get_current_user, require_admin
from app.admin import _get_user_project_with_role
from app.resource_envelope import (
    assess_ceiling_gate_trustworthiness,
    get_project_recent_spend_usd,
    resolve_project_hardware_policy,
    resolve_project_pause_on_ceiling_exceeded,
    resolve_project_require_local_runner,
    resolve_project_spend_ceiling,
    resolve_project_spend_warning_fraction,
    resolve_project_spend_window_days,
    resolve_project_vm_cap,
)
from app.runtime_config_state import (
    resolve_project_compute_job_concurrency_cap,
    runtime_config as _runtime_config,
)
from app.schemas import ResourceEnvelope as ResourceEnvelopeSchema
from app.task_locking import lock_project_or_429, with_design_doc_locked
from app.playbook_storage import archive_playbook_entry
from app.alert_policy import (
    apply_project_alert_domain_updates,
    serialize_project_alert_domains,
)
from app.shared_state import (
    backfill_branch_columns,
    backfill_task_branch_namespace,
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
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    by_type: Dict[str, Any] = {}
    total_cost = 0.0

    # Stream rows to avoid loading the full date range into memory at once.
    # (func.greatest and func.extract('epoch', interval) are PostgreSQL-only so
    # aggregation stays in Python; yield_per caps the per-fetch batch size.)
    stream = await session.stream(
        select(
            Task.provider,
            Task.vm_spot,
            Task.vm_machine_type,
            Task.vm_provisioned_at,
            Task.vm_stopped_at,
        ).where(
            Task.created_at >= cutoff,
            Task.vm_provisioned_at.is_not(None),
            Task.vm_stopped_at.is_not(None),
        )
    )

    async for row in stream:
        provider = row.provider
        vm_spot = row.vm_spot
        machine_type = row.vm_machine_type
        key = f"{provider or 'unknown'}:{'spot' if vm_spot else 'on-demand'}"
        duration = max(0.0, (row.vm_stopped_at - row.vm_provisioned_at).total_seconds())
        if machine_type and machine_type.startswith("cloud-run-"):
            rate = get_cloud_run_rate_for_machine_type(machine_type)
        else:
            rate = get_vm_rate_per_second(machine_type=machine_type, spot=vm_spot)
        estimated_cost = duration * rate
        existing = by_type.get(key)
        if existing is not None:
            existing["count"] += 1
            existing["total_seconds"] += duration
            existing["estimated_cost"] += estimated_cost
        else:
            by_type[key] = {
                "count": 1,
                "total_seconds": duration,
                "estimated_cost": estimated_cost,
                "provider": provider,
                "spot": vm_spot,
            }
        total_cost += estimated_cost

    for row in by_type.values():
        row["estimated_cost"] = float(row["estimated_cost"])
        row["total_seconds"] = float(row["total_seconds"])

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
                    Task.auto_remediation.is_(True),
                )
                .options(
                    load_only(
                        Task.id,
                        Task.status,
                        Task.title,
                        Task.auto_remediation,
                        Task.created_at,
                        raiseload=True,
                    )
                )
                .order_by(Task.created_at.desc())
                .limit(500)
            )
        )
        .scalars()
        .all()
    )

    auto_tasks = [t for t in recent_rows if t.auto_remediation is True]
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

    # Only the 5 most-recent preview rows expose ci_failure_branch, so fetch the
    # (large) context JSONB just for those ids in a narrow second query rather
    # than materializing context for all 500 rows scanned above. The counts and
    # success rate computed above need no JSONB and are derived without it.
    preview_tasks = auto_tasks[:5]
    branch_by_id: Dict[Any, str] = {}
    if preview_tasks:
        preview_ids = [t.id for t in preview_tasks]
        ctx_rows = (
            await session.execute(
                select(Task.id, Task.context).where(Task.id.in_(preview_ids))
            )
        ).all()
        for row in ctx_rows:
            branch_by_id[row[0]] = (row[1] or {}).get("ci_failure_branch", "")

    recent_tasks_data = [
        {
            "id": str(t.id),
            "title": t.title or "",
            "status": t.status,
            "branch": branch_by_id.get(t.id, ""),
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in preview_tasks
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


async def _validate_branch_exists_on_remote(project, branch: str) -> None:
    """Confirm that branch exists on the project's GitHub remote.

    Raises HTTP 422 if the branch is not found, HTTP 502 if the GitHub
    credential cannot be resolved.  Does nothing when the project has no
    GitHub integration configured (github_owner/github_repo are None).
    """
    if not project.github_owner or not project.github_repo:
        return

    from app.github import get_branch_sha, get_repo_installation_token

    token = await get_repo_installation_token(project.github_owner, project.github_repo)
    if token is None:
        raise HTTPException(
            status_code=502,
            detail=(
                f"GitHub credential unavailable for "
                f"{project.github_owner}/{project.github_repo} — "
                "cannot validate branch existence"
            ),
        )

    try:
        await get_branch_sha(token, project.github_owner, project.github_repo, branch)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(
                status_code=422,
                detail=(
                    f'Branch "{branch}" does not exist on '
                    f"{project.github_owner}/{project.github_repo}"
                ),
            )
        raise


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
    precheck_project = await _get_user_project_with_role(
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
        # Reject integration == promotion BEFORE persisting. Landing integration
        # work directly on the promotion/release target is unsafe, and the
        # resolver fails safe rather than raising, so an invalid value would
        # otherwise persist silently. Validate up front and return a clean 422
        # without writing the column (or making a remote round-trip).
        promotion_target = get_project_promotion_target(precheck_project)
        if branch == promotion_target:
            raise HTTPException(
                status_code=422,
                detail=(
                    "'integration_branch' must differ from the promotion target "
                    f"'{promotion_target}'; landing integration work directly on "
                    "the promotion target is not allowed."
                ),
            )
        await _validate_branch_exists_on_remote(precheck_project, branch)

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
    precheck_project = await _get_user_project_with_role(
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
        await _validate_branch_exists_on_remote(precheck_project, branch)

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
# Approval-gate config endpoints
# ---------------------------------------------------------------------------

_APPROVAL_GATE_KEYS = ("infra_files", "critical_files")

# A plausible repo-relative path: forward-slash separated, no leading slash,
# no parent-directory traversal, no shell metacharacters.
_VALID_REPO_PATH_RE = re.compile(r"^[A-Za-z0-9._\-/]+$")


def _validate_approval_gate_paths(field: str, value) -> list[str]:
    """Validate and normalise one approval-gate file list, returning trimmed paths."""
    if not isinstance(value, list):
        raise HTTPException(
            status_code=422,
            detail=f"'{field}' must be a list of repo-relative path strings",
        )
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise HTTPException(
                status_code=422,
                detail=f"'{field}' entries must be non-empty strings",
            )
        path = item.strip()
        if (
            path.startswith("/")
            or ".." in path.split("/")
            or not _VALID_REPO_PATH_RE.match(path)
        ):
            raise HTTPException(
                status_code=422,
                detail=f"'{field}' entry {item!r} is not a valid repo-relative path",
            )
        cleaned.append(path)
    return cleaned


def _approval_gate_response(project_id: uuid.UUID, config: dict) -> Dict[str, Any]:
    return {
        "project_id": str(project_id),
        "approval_gate_config": {
            "infra_files": list(config.get("infra_files", [])),
            "critical_files": list(config.get("critical_files", [])),
        },
        "is_default": not config,
    }


@router.get("/projects/{project_id}/approval-gate-config")
async def get_approval_gate_config(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the per-project approval-gate file lists (infra_files / critical_files)."""
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    return _approval_gate_response(project_id, dict(project.approval_gate_config or {}))


@router.patch("/projects/{project_id}/approval-gate-config")
async def update_approval_gate_config(
    project_id: uuid.UUID,
    body: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Set the per-project approval-gate file lists (partial update, owner-only).

    Takes effect for agents immediately via the existing claim-response seam.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    unknown = set(body.keys()) - set(_APPROVAL_GATE_KEYS)
    if unknown:
        raise HTTPException(
            status_code=422, detail=f"Unknown config fields: {sorted(unknown)}"
        )

    cleaned = {
        key: _validate_approval_gate_paths(key, body[key])
        for key in _APPROVAL_GATE_KEYS
        if key in body
    }

    # Lock project row before read-modify-write to prevent lost updates.
    project = await lock_project_or_429(session, project_id)
    config = dict(project.approval_gate_config or {})
    config.update(cleaned)
    project.approval_gate_config = config
    flag_modified(project, "approval_gate_config")
    session.add(project)
    await session.commit()

    return _approval_gate_response(project_id, config)


# ---------------------------------------------------------------------------
# Task-branch namespace / prefix config endpoints
# ---------------------------------------------------------------------------


def _build_config_has_key(project, key: str) -> bool:
    value = (project.build_config or {}).get(key)
    return value not in (None, "")


def _resource_envelope_response(
    project,
    *,
    active_vm_count: int,
    spend_breakdown: dict,
    trustworthiness: dict | None = None,
) -> Dict[str, Any]:
    spend_ceiling_usd = resolve_project_spend_ceiling(project)
    recent_spend_usd = spend_breakdown["total"]
    allowed_machines, allowed_accels = resolve_project_hardware_policy(project)
    # Additive, backward-compatible confidence signal for the weighted spend
    # figure. Existing keys keep their meaning/type; enforcement still gates on
    # ``ceiling_gate_total`` unchanged. ``coverage_fraction`` (the independent
    # Anthropic probe coverage) comes from the optional trustworthiness verdict.
    ceiling_gate_total = spend_breakdown.get(
        "ceiling_gate_total", recent_spend_usd
    )
    spend_confidence = {
        "confidence": spend_breakdown.get("confidence", "verified"),
        "total_usd": float(recent_spend_usd),
        "ceiling_gate_total_usd": float(ceiling_gate_total),
        "llm_anthropic_usd": float(spend_breakdown.get("llm_anthropic", 0.0)),
        "llm_non_anthropic_usd": float(
            spend_breakdown.get("llm_non_anthropic", 0.0)
        ),
        "non_anthropic_share": float(
            spend_breakdown.get("non_anthropic_share", 0.0)
        ),
        "coverage_fraction": (
            trustworthiness.get("coverage_fraction")
            if isinstance(trustworthiness, dict)
            else None
        ),
    }
    return {
        "project_id": str(project.id),
        "max_concurrent_vms": resolve_project_vm_cap(project),
        "spend_ceiling_usd": spend_ceiling_usd,
        "spend_window_days": resolve_project_spend_window_days(project),
        "spend_warning_fraction": resolve_project_spend_warning_fraction(project),
        "max_concurrent_compute_jobs": resolve_project_compute_job_concurrency_cap(
            project.id
        ),
        "allowed_machine_types": allowed_machines,
        "allowed_accelerator_types": allowed_accels,
        "require_local_runner": resolve_project_require_local_runner(project),
        "pause_on_ceiling_exceeded": resolve_project_pause_on_ceiling_exceeded(project),
        "active_vm_count": active_vm_count,
        "recent_spend_usd": float(recent_spend_usd),
        "recent_spend_compute_usd": float(spend_breakdown["compute"]),
        "recent_spend_agent_vm_usd": float(spend_breakdown["agent_vm"]),
        "recent_spend_llm_usd": float(spend_breakdown["llm"]),
        "recent_spend_idle_usd": float(spend_breakdown["idle"]),
        "is_throttled": (
            spend_ceiling_usd is not None and recent_spend_usd >= spend_ceiling_usd
        ),
        "spend_cap_confidence": spend_confidence,
    }


async def _project_spend_trustworthiness(
    session: AsyncSession, project_id: uuid.UUID, window_days: int
) -> dict:
    """Assess the weighted spend figure against the project's real window tasks.

    Reconciles the Anthropic portion (the only independently-probed component)
    over the SAME task window the ceiling gate aggregates, so the surfaced
    ``coverage_fraction`` reflects real historical spend, not a synthetic set.
    """
    from app.resource_envelope import get_project_spend_window_tasks

    tasks = await get_project_spend_window_tasks(
        session, project_id=project_id, window_days=window_days
    )
    return assess_ceiling_gate_trustworthiness(tasks)


async def _get_project_active_vm_count(
    session: AsyncSession, project_id: uuid.UUID
) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Task)
        .where(
            Task.project_id == project_id,
            Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
        )
    )
    return int(result.scalar() or 0)


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
# No-CI honest-block policy config endpoints
#
# DEFAULT-OFF per-project opt-in: when ON, the no-CI merge gate BLOCKS a PR
# (holds it open) instead of merging it UNVERIFIED whenever the gate EXPECTED to
# verify the code but could NOT run the repo's suite (a DEGRADED fall-open
# reason). Absent/False == today's behavior. Member-read / owner-write, stored
# in the build_config JSONB column (migration-free) alongside requires_ci_checks.
# ---------------------------------------------------------------------------


def _block_degraded_unverified_merge_value(project) -> bool:
    return bool(
        (project.build_config or {}).get("block_degraded_unverified_merge", False)
    )


@router.get("/projects/{project_id}/block-degraded-unverified-merge")
async def get_block_degraded_unverified_merge_config(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the per-project no-CI honest-block policy flag (default OFF)."""
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    return {
        "project_id": str(project_id),
        "block_degraded_unverified_merge": _block_degraded_unverified_merge_value(
            project
        ),
        "is_default": not _build_config_has_key(
            project, "block_degraded_unverified_merge"
        ),
    }


@router.patch("/projects/{project_id}/block-degraded-unverified-merge")
async def update_block_degraded_unverified_merge_config(
    project_id: uuid.UUID,
    body: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Set the per-project no-CI honest-block policy. Pass false to revert to the
    default (merge UNVERIFIED on a degraded fall-open, today's behavior)."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    if "block_degraded_unverified_merge" not in body:
        raise HTTPException(
            status_code=422,
            detail="Request body must include 'block_degraded_unverified_merge'",
        )

    enabled = body["block_degraded_unverified_merge"]
    if not isinstance(enabled, bool):
        raise HTTPException(
            status_code=422,
            detail="'block_degraded_unverified_merge' must be a boolean",
        )

    project = await lock_project_or_429(session, project_id)
    build_config = dict(project.build_config or {})
    if enabled:
        build_config["block_degraded_unverified_merge"] = True
    else:
        # Pop on False so absent == default == OFF (keeps build_config lean and
        # is_default semantically correct).
        build_config.pop("block_degraded_unverified_merge", None)
    project.build_config = build_config
    flag_modified(project, "build_config")
    session.add(project)
    await session.commit()

    return {
        "project_id": str(project_id),
        "block_degraded_unverified_merge": _block_degraded_unverified_merge_value(
            project
        ),
        "is_default": not _build_config_has_key(
            project, "block_degraded_unverified_merge"
        ),
    }


# ---------------------------------------------------------------------------
# block-no-test-unverified-merge config endpoints
# ---------------------------------------------------------------------------


def _block_no_test_unverified_merge_value(project) -> bool:
    return bool(
        (project.build_config or {}).get("block_no_test_unverified_merge", False)
    )


@router.get("/projects/{project_id}/block-no-test-unverified-merge")
async def get_block_no_test_unverified_merge_config(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the per-project no-test honest-block policy flag (default OFF)."""
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    return {
        "project_id": str(project_id),
        "block_no_test_unverified_merge": _block_no_test_unverified_merge_value(
            project
        ),
        "is_default": not _build_config_has_key(
            project, "block_no_test_unverified_merge"
        ),
    }


@router.patch("/projects/{project_id}/block-no-test-unverified-merge")
async def update_block_no_test_unverified_merge_config(
    project_id: uuid.UUID,
    body: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Set the per-project no-test honest-block policy. Pass false to revert to
    the default (merge UNVERIFIED when no test command is detectable)."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    if "block_no_test_unverified_merge" not in body:
        raise HTTPException(
            status_code=422,
            detail="Request body must include 'block_no_test_unverified_merge'",
        )

    enabled = body["block_no_test_unverified_merge"]
    if not isinstance(enabled, bool):
        raise HTTPException(
            status_code=422,
            detail="'block_no_test_unverified_merge' must be a boolean",
        )

    project = await lock_project_or_429(session, project_id)
    build_config = dict(project.build_config or {})
    if enabled:
        build_config["block_no_test_unverified_merge"] = True
    else:
        # Pop on False so absent == default == OFF (keeps build_config lean and
        # is_default semantically correct).
        build_config.pop("block_no_test_unverified_merge", None)
    project.build_config = build_config
    flag_modified(project, "build_config")
    session.add(project)
    await session.commit()

    return {
        "project_id": str(project_id),
        "block_no_test_unverified_merge": _block_no_test_unverified_merge_value(
            project
        ),
        "is_default": not _build_config_has_key(
            project, "block_no_test_unverified_merge"
        ),
    }


# ---------------------------------------------------------------------------
# Staging-branch-prefixes config endpoints
# ---------------------------------------------------------------------------

_DEFAULT_STAGING_BRANCH_PREFIXES = ["research/", "plan/"]


def _get_staging_branch_prefixes(project) -> list[str]:
    configured = (project.build_config or {}).get("staging_branch_prefixes")
    if configured is not None:
        prefixes = [str(p).strip() for p in configured if str(p).strip()]
        if prefixes:
            return prefixes
    return list(_DEFAULT_STAGING_BRANCH_PREFIXES)


@router.get("/projects/{project_id}/staging-branch-prefixes")
async def get_staging_branch_prefixes_config(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the per-project staging-branch prefixes (gates rollup/auto-merge routing)."""
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    return {
        "project_id": str(project_id),
        "staging_branch_prefixes": _get_staging_branch_prefixes(project),
        "is_default": not _build_config_has_key(project, "staging_branch_prefixes"),
    }


@router.patch("/projects/{project_id}/staging-branch-prefixes")
async def update_staging_branch_prefixes_config(
    project_id: uuid.UUID,
    body: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Set the per-project staging-branch prefixes. Pass null to revert to the global default."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    if "staging_branch_prefixes" not in body:
        raise HTTPException(
            status_code=422,
            detail="Request body must include 'staging_branch_prefixes'",
        )

    prefixes = body["staging_branch_prefixes"]
    if prefixes is not None:
        if not isinstance(prefixes, list):
            raise HTTPException(
                status_code=422,
                detail="'staging_branch_prefixes' must be a list of strings or null",
            )
        trimmed = [p.strip() if isinstance(p, str) else None for p in prefixes]
        if any(p is None or not p for p in trimmed):
            raise HTTPException(
                status_code=422,
                detail="'staging_branch_prefixes' must not contain empty or whitespace-only entries",
            )
        if not trimmed:
            raise HTTPException(
                status_code=422,
                detail="'staging_branch_prefixes' must contain at least one prefix",
            )
        prefixes = trimmed

    project = await lock_project_or_429(session, project_id)
    build_config = dict(project.build_config or {})
    if prefixes is None:
        build_config.pop("staging_branch_prefixes", None)
    else:
        build_config["staging_branch_prefixes"] = prefixes
    project.build_config = build_config
    flag_modified(project, "build_config")
    session.add(project)
    await session.commit()

    return {
        "project_id": str(project_id),
        "staging_branch_prefixes": _get_staging_branch_prefixes(project),
        "is_default": not _build_config_has_key(project, "staging_branch_prefixes"),
    }


# ---------------------------------------------------------------------------
# Resource-envelope config endpoints
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/resource-envelope")
async def get_resource_envelope_config(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the project resource envelope and live usage snapshot."""
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    window_days = resolve_project_spend_window_days(project)
    active_vm_count = await _get_project_active_vm_count(session, project_id)
    spend_breakdown = await get_project_recent_spend_usd(
        session,
        project_id=project_id,
        window_days=window_days,
    )
    trustworthiness = await _project_spend_trustworthiness(
        session, project_id, window_days
    )
    return _resource_envelope_response(
        project,
        active_vm_count=active_vm_count,
        spend_breakdown=spend_breakdown,
        trustworthiness=trustworthiness,
    )


@router.patch("/projects/{project_id}/resource-envelope")
async def update_resource_envelope_config(
    project_id: uuid.UUID,
    body: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Update the per-project resource envelope stored in build_config."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    allowed = {
        "max_concurrent_vms",
        "spend_ceiling_usd",
        "spend_window_days",
        "spend_warning_fraction",
        "max_concurrent_compute_jobs",
        "allowed_machine_types",
        "allowed_accelerator_types",
        "require_local_runner",
        "pause_on_ceiling_exceeded",
    }
    if not body:
        raise HTTPException(
            status_code=422,
            detail="Request body must include at least one resource envelope field",
        )
    unknown = set(body.keys()) - allowed
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown resource envelope fields: {sorted(unknown)}",
        )
    project = await lock_project_or_429(session, project_id)
    existing_envelope = (project.build_config or {}).get("resource_envelope")
    if not isinstance(existing_envelope, dict):
        existing_envelope = {}

    merged = dict(existing_envelope)
    for key in allowed:
        if key in body:
            merged[key] = body[key]

    try:
        validated = ResourceEnvelopeSchema(**merged)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail="Invalid resource envelope"
        ) from exc

    # max_concurrent_compute_jobs lives in runtime_config, not build_config.
    # Update in-memory and persist to DeploymentState.runtime_config_json.
    if "max_concurrent_compute_jobs" in body:
        job_cap = validated.max_concurrent_compute_jobs
        job_caps = dict(
            _runtime_config.get("max_concurrent_compute_jobs_by_project") or {}
        )
        sid = str(project_id)
        if job_cap is None:
            job_caps.pop(sid, None)
        else:
            job_caps[sid] = job_cap
        _runtime_config["max_concurrent_compute_jobs_by_project"] = job_caps
        # Persist so the cap survives restarts.
        state_result = await session.execute(
            select(DeploymentState).where(DeploymentState.id == 1)
        )
        state = state_result.scalar_one_or_none()
        if state is not None:
            existing_json = dict(state.runtime_config_json or {})
            existing_json["max_concurrent_compute_jobs_by_project"] = {
                k: v for k, v in job_caps.items() if v and int(v) > 0
            }
            state.runtime_config_json = existing_json
            flag_modified(state, "runtime_config_json")
            session.add(state)

    # Strip max_concurrent_compute_jobs from the build_config envelope —
    # it is stored in runtime_config, not build_config.
    envelope_dump = validated.model_dump(exclude_none=True)
    envelope_dump.pop("max_concurrent_compute_jobs", None)

    build_config = dict(project.build_config or {})
    build_config["resource_envelope"] = envelope_dump
    project.build_config = build_config
    flag_modified(project, "build_config")
    session.add(project)
    await session.commit()
    await session.refresh(project)

    window_days = resolve_project_spend_window_days(project)
    active_vm_count = await _get_project_active_vm_count(session, project_id)
    spend_breakdown = await get_project_recent_spend_usd(
        session,
        project_id=project_id,
        window_days=window_days,
    )
    trustworthiness = await _project_spend_trustworthiness(
        session, project_id, window_days
    )
    return _resource_envelope_response(
        project,
        active_vm_count=active_vm_count,
        spend_breakdown=spend_breakdown,
        trustworthiness=trustworthiness,
    )


# ---------------------------------------------------------------------------
# Alert-domain config endpoints
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/alert-domains")
async def get_alert_domains_config(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return reusable alert-domain enablement for a project."""
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    return serialize_project_alert_domains(project)


@router.patch("/projects/{project_id}/alert-domains")
async def update_alert_domains_config(
    project_id: uuid.UUID,
    body: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Enable, disable, or reset project-specific alert domains.

    Body shape:
      {"domains": {"warm_pool": {"enabled": true}, "sr_quality": null}}
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    if "domains" not in body or not isinstance(body["domains"], dict):
        raise HTTPException(
            status_code=422,
            detail="Request body must include object field 'domains'",
        )
    project = await lock_project_or_429(session, project_id)
    try:
        response = apply_project_alert_domain_updates(project, body["domains"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    flag_modified(project, "build_config")
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return response


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
# Full-suite drift sentinel config endpoints
# ---------------------------------------------------------------------------


def _full_suite_drift_sentinel_value(project) -> bool:
    return bool(
        (project.build_config or {}).get("full_suite_drift_sentinel_enabled", False)
    )


@router.get("/projects/{project_id}/full-suite-drift-sentinel")
async def get_full_suite_drift_sentinel_config(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return whether the standing full-suite CI drift sentinel is enabled (default OFF)."""
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    return {
        "project_id": str(project_id),
        "full_suite_drift_sentinel_enabled": _full_suite_drift_sentinel_value(project),
        "is_default": not _build_config_has_key(project, "full_suite_drift_sentinel_enabled"),
    }


@router.patch("/projects/{project_id}/full-suite-drift-sentinel")
async def update_full_suite_drift_sentinel_config(
    project_id: uuid.UUID,
    body: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Enable or disable the standing full-suite CI drift sentinel (owner-only).

    When enabled, each reaper sweep dispatches ci.yml on the integration branch
    for every open stabilize-phase version and alerts + spawns harvest remediation
    when a concluded run is red.  Pass ``false`` to revert to the default (OFF).
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    if "full_suite_drift_sentinel_enabled" not in body:
        raise HTTPException(
            status_code=422,
            detail="Request body must include 'full_suite_drift_sentinel_enabled'",
        )

    enabled = body["full_suite_drift_sentinel_enabled"]
    if not isinstance(enabled, bool):
        raise HTTPException(
            status_code=422,
            detail="'full_suite_drift_sentinel_enabled' must be a boolean",
        )

    project = await lock_project_or_429(session, project_id)
    build_config = dict(project.build_config or {})
    if enabled:
        build_config["full_suite_drift_sentinel_enabled"] = True
    else:
        build_config.pop("full_suite_drift_sentinel_enabled", None)
    project.build_config = build_config
    flag_modified(project, "build_config")
    session.add(project)
    await session.commit()

    return {
        "project_id": str(project_id),
        "full_suite_drift_sentinel_enabled": _full_suite_drift_sentinel_value(project),
        "is_default": not _build_config_has_key(project, "full_suite_drift_sentinel_enabled"),
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


# ---------------------------------------------------------------------------
# Branch-column backfill endpoint
# ---------------------------------------------------------------------------


@router.post("/projects/{project_id}/backfill-branches")
async def backfill_project_branches(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Re-run the integration_branch / promotion_target backfill for a single project.

    Only writes NULL columns — populated columns are left untouched.  Useful when
    a project was onboarded before migrations 0124/0125 or when the initial GitHub
    API call failed and the operator has since fixed credentials.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    summary = await backfill_branch_columns(session, project_id=project_id)
    ns_summary = await backfill_task_branch_namespace(session, project_id=project_id)
    await session.commit()
    return {"project_id": str(project_id), **summary, **ns_summary}
