from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, delete
from typing import Dict, Any, List
import uuid
from datetime import timedelta
from sqlalchemy import func
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_session
from app.shared_state import limiter
from app.models import Task, TaskStatus, Project, ProjectMember, _utcnow
from app.auth import get_current_user
from app.admin import _get_user_project, _get_user_project_with_role, _user_project_ids
from app.task_locking import lock_project_or_429

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-projects"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/projects")
@limiter.limit("30/minute")
async def get_all_projects(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    # Select only lightweight columns — large JSONB (directions, vision, design_doc,
    # problem_model, self_reflection_log, running_update) are excluded here and
    # available via GET /api/admin/projects/{project_id}.
    stmt = (
        select(
            Project.id,
            Project.name,
            Project.created_at,
            Project.status,
            Project.user_id,
            Project.github_owner,
            Project.github_repo,
            Project.is_self_repo,
            Project.autonomous_enabled,
            Project.build_config,
            Project.sr_catch_rate_targets,
            Project.running_update_acknowledged_at,
            func.coalesce(func.jsonb_array_length(Project.directions), 0).label(
                "directions_count"
            ),
            ProjectMember.role.label("my_role"),
        )
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(ProjectMember.user_id == current_user["user_id"])
        .order_by(Project.created_at.desc())
        .limit(limit + 1)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).all()
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    return {
        "items": [
            {
                "id": str(row.id),
                "name": row.name,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "status": row.status,
                "user_id": row.user_id,
                "github_owner": row.github_owner,
                "github_repo": row.github_repo,
                "is_self_repo": row.is_self_repo,
                "autonomous_enabled": row.autonomous_enabled,
                "build_config": row.build_config or {},
                "sr_catch_rate_targets": row.sr_catch_rate_targets or {},
                "running_update_acknowledged_at": (
                    row.running_update_acknowledged_at.isoformat()
                    if row.running_update_acknowledged_at
                    else None
                ),
                "directions_count": row.directions_count,
                "my_role": row.my_role,
            }
            for row in page_rows
        ],
        "has_more": has_more,
    }


@router.get("/projects/{project_id}")
@limiter.limit("30/minute")
async def get_project(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the full project record including all JSONB detail columns."""
    query = (
        select(Project, ProjectMember.role)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(
            Project.id == project_id,
            ProjectMember.user_id == current_user["user_id"],
        )
    )
    row = (await session.execute(query)).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    project, role = row
    return {
        "id": str(project.id),
        "name": project.name,
        "user_id": project.user_id,
        "status": project.status,
        "github_owner": project.github_owner,
        "github_repo": project.github_repo,
        "created_at": project.created_at.isoformat() if project.created_at else None,
        "directions": project.directions,
        "vision": project.vision or {},
        "design_doc": project.design_doc or {},
        "problem_model": project.problem_model or {},
        "running_update": project.running_update,
        "running_update_acknowledged_at": (
            project.running_update_acknowledged_at.isoformat()
            if project.running_update_acknowledged_at
            else None
        ),
        "self_reflection_log": project.self_reflection_log or [],
        "build_config": project.build_config or {},
        "autonomous_enabled": project.autonomous_enabled,
        "is_self_repo": project.is_self_repo,
        "sr_catch_rate_targets": project.sr_catch_rate_targets or {},
        "my_role": role,
    }


class ProjectUpdate(BaseModel):
    name: str | None = None
    directions: List[Dict[str, Any]] | None = None
    vision: Dict[str, Any] | None = None
    design_doc: Dict[str, Any] | None = None
    build_config: Dict[str, Any] | None = None
    autonomous_enabled: bool | None = None
    # Per-category SR catch-rate targets, e.g. {'security': 0.80}.
    # Absent key = no target; empty dict {} clears all targets.
    sr_catch_rate_targets: Dict[str, Any] | None = None
    # User-editable subset of problem_model — intent, constraints, context.
    # Gaps / capabilities / friction are agent-owned and not writable here.
    problem_model_patch: Dict[str, Any] | None = None


@router.patch("/projects/{project_id}")
@limiter.limit("30/minute")
async def update_project(
    request: Request,
    project_id: uuid.UUID,
    payload: ProjectUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    if payload.name is not None:
        project.name = payload.name
    if payload.autonomous_enabled is not None:
        project.autonomous_enabled = payload.autonomous_enabled
    if payload.directions is not None:
        # Auto-generate ids for any directions missing one
        normalized = []
        for d in payload.directions:
            entry = dict(d)
            if not entry.get("id"):
                entry["id"] = f"d-{uuid.uuid4().hex[:8]}"
            if entry.get("priority") not in ("high", "medium", "low"):
                raise HTTPException(
                    status_code=400, detail=f"Invalid priority: {entry.get('priority')}"
                )
            if not isinstance(entry.get("text"), str) or not entry["text"].strip():
                raise HTTPException(
                    status_code=400, detail="Each direction must have non-empty text"
                )
            normalized.append(entry)
        project.directions = normalized
        flag_modified(project, "directions")
    if payload.vision is not None:
        vision = dict(payload.vision)
        current_rev = (project.vision or {}).get("revision", 0)
        vision["revision"] = (current_rev or 0) + 1
        vision["last_updated"] = _utcnow().isoformat()
        project.vision = vision
        flag_modified(project, "vision")
    if payload.design_doc is not None:
        doc = dict(payload.design_doc)
        current_rev = (project.design_doc or {}).get("revision", 0)
        doc["revision"] = (current_rev or 0) + 1
        doc["last_updated"] = _utcnow().isoformat()
        doc["updated_by"] = "user"
        project.design_doc = doc
        flag_modified(project, "design_doc")
    if payload.build_config is not None:
        from pydantic import ValidationError

        from app.schemas import BuildConfig as BuildConfigSchema

        try:
            merged = {**(project.build_config or {}), **payload.build_config}
            validated = BuildConfigSchema(**merged) if merged else BuildConfigSchema()
            project.build_config = validated.model_dump(exclude_none=True)
        except ValidationError as e:
            raise HTTPException(
                status_code=400, detail="Invalid build_config structure"
            ) from e
        flag_modified(project, "build_config")

    if payload.sr_catch_rate_targets is not None:
        # Validate: values must be floats in [0, 1]
        for cat, val in payload.sr_catch_rate_targets.items():
            if not isinstance(val, (int, float)) or not (0.0 <= float(val) <= 1.0):
                raise HTTPException(
                    status_code=400,
                    detail=f"sr_catch_rate_targets[{cat!r}] must be a float in [0, 1]",
                )
        project.sr_catch_rate_targets = {
            k: float(v) for k, v in payload.sr_catch_rate_targets.items()
        }
        flag_modified(project, "sr_catch_rate_targets")

    if payload.problem_model_patch is not None:
        # User-editable subset only — intent / context / constraints. Agent-
        # owned fields (gaps, capabilities, friction, archived_gaps,
        # pending_user_confirmation, revision_log) pass through unchanged.
        # Bump revision, stamp last_updated, mark last_updated_by="user"
        # so the agent can distinguish user edits during its next cycle.
        from pydantic import ValidationError as _PmValidationError

        from app.problem_model import ProblemModel, parse_problem_model

        ALLOWED_USER_FIELDS = {"intent", "context", "constraints"}
        invalid = set(payload.problem_model_patch.keys()) - ALLOWED_USER_FIELDS
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot edit agent-owned problem_model fields: {sorted(invalid)}. "
                f"Editable fields: {sorted(ALLOWED_USER_FIELDS)}",
            )
        current = parse_problem_model(project.problem_model)
        merged_raw: Dict[str, Any] = current.model_dump()
        merged_raw.update(payload.problem_model_patch)
        try:
            validated = ProblemModel.model_validate(merged_raw)
        except _PmValidationError as ve:
            raise HTTPException(
                status_code=400, detail=f"Invalid problem_model patch: {ve}"
            ) from None
        validated.revision = current.revision + 1
        validated.last_updated = _utcnow().isoformat()
        validated.last_updated_by = "user"
        project.problem_model = validated.model_dump()
        flag_modified(project, "problem_model")

    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


@router.post("/projects/{project_id}/detect-build-config")
@limiter.limit("10/minute")
async def detect_project_build_config(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    if not project.github_owner or not project.github_repo:
        raise HTTPException(
            status_code=400, detail="Project has no GitHub repository configured"
        )

    from pydantic import ValidationError

    from app.github import get_repo_installation_token
    from app.routers.admin_onboard import _detect_build_config
    from app.schemas import BuildConfig as BuildConfigSchema

    token = await get_repo_installation_token(project.github_owner, project.github_repo)
    if not token:
        raise HTTPException(
            status_code=502,
            detail="No GitHub installation token available for this repo",
        )

    detected = await _detect_build_config(
        token, project.github_owner, project.github_repo
    )

    existing = project.build_config or {}
    if existing.get("git_user_email"):
        detected["git_user_email"] = existing["git_user_email"]
    if existing.get("git_user_name"):
        detected["git_user_name"] = existing["git_user_name"]

    try:
        validated = BuildConfigSchema(**detected) if detected else BuildConfigSchema()
        project.build_config = validated.model_dump(exclude_none=True)
    except ValidationError:
        # Detection produced a shape the schema doesn't accept — store raw so the user can fix it in the UI
        project.build_config = detected
    flag_modified(project, "build_config")
    await session.commit()
    await session.refresh(project)

    return {"build_config": project.build_config or {}}


@router.post("/projects/{project_id}/running-update/acknowledge")
@limiter.limit("30/minute")
async def acknowledge_running_update(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Acknowledge the project's running_update — stamps the read-marker
    timestamp. The document itself persists; the strategic agent refines
    it on the next cycle using the acknowledgement timestamp to frame
    what's new since the user last caught up."""
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    project.running_update_acknowledged_at = _utcnow()
    session.add(project)
    await session.commit()
    return {
        "status": "acknowledged",
        "acknowledged_at": project.running_update_acknowledged_at.isoformat(),
    }


@router.delete("/projects/{project_id}")
@limiter.limit("10/minute")
async def delete_project_admin(
    request: Request,
    project_id: uuid.UUID,
    force: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Delete a project and cascade to tasks/artifacts/history.
    Refuses if active tasks exist unless force=true."""
    # Phase C2 (L-C3): role check first (404/403 before lock contention surfaces
    # as 429), then acquire a Project row lock with skip_locked. Closes the
    # heartbeat-arrives-mid-delete race where a concurrent agent status POST
    # could mutate task rows between our cancel and FK-cascade delete.
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    project = await lock_project_or_429(session, project_id)

    ACTIVE_STATUSES = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    ]
    active_tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id, Task.status.in_(ACTIVE_STATUSES)
                )
            )
        )
        .scalars()
        .all()
    )

    if active_tasks and not force:
        raise HTTPException(
            status_code=409,
            detail=f"Project has {len(active_tasks)} active task(s). Cancel them first, or pass ?force=true.",
        )

    if active_tasks and force:
        # Force: cancel active tasks and delete their VMs
        from app.main import _ensure_vm_deleted

        for task in active_tasks:
            task.status = TaskStatus.CANCELLED
            task.latest_status_message = "Project force-deleted"
            session.add(task)
            await _ensure_vm_deleted(task, reason="project_force_delete")
        await session.commit()

    # Delete dependent records that lack ON DELETE CASCADE
    from app.models import Alert

    await session.execute(delete(Alert).where(Alert.project_id == project_id))
    await session.delete(project)
    await session.commit()

    # Evict from GitHub token cache
    from app.routers.agent import _github_token_cache

    _github_token_cache.pop(str(project_id), None)

    return {
        "message": f"Project {project_id} deleted",
        "active_tasks_cancelled": len(active_tasks) if active_tasks else 0,
    }


@router.post("/autonomous/pause-all")
@limiter.limit("10/minute")
async def pause_all_autonomous(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Disable autonomous mode on all projects at once."""
    projects = (
        (
            await session.execute(
                select(Project).where(
                    Project.id.in_(_user_project_ids(current_user["user_id"]))
                )
            )
        )
        .scalars()
        .all()
    )
    for p in projects:
        p.autonomous_enabled = False
    await session.commit()
    return {"paused": len(projects)}


@router.get("/projects/{project_id}/directions/task-counts")
@limiter.limit("30/minute")
async def get_direction_task_counts(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    await _get_user_project(project_id, current_user["user_id"], session)

    result = await session.execute(
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status.not_in([TaskStatus.CANCELLED, TaskStatus.FAILED]),
        )
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.payloads,
                Task.context,
                Task.title,
                Task.latest_status_message,
            )
        )
        .limit(5000)
    )
    tasks = result.scalars().all()

    counts: Dict[str, int] = {}
    counts_by_type: Dict[str, Dict[str, int]] = {}
    active_tasks: Dict[str, list[dict]] = {}
    for task in tasks:
        payload = task.first_payload
        # Primary: context.direction_id (matches admin_digest.py behavior).
        # Fallback: payload.current_direction_id, then payload.directions[].id
        # (for older tasks without context).
        dir_ids: list[str] = []
        ctx_did = (task.context or {}).get("direction_id")
        if ctx_did:
            dir_ids.append(ctx_did)
        else:
            current_did = payload.get("current_direction_id")
            if current_did:
                dir_ids.append(current_did)
            else:
                for direction in payload.get("directions", []):
                    d_id = direction.get("id")
                    if d_id:
                        dir_ids.append(d_id)
        if not dir_ids:
            continue
        task_type = payload.get("type") or "unknown"
        for d_id in dir_ids:
            counts[d_id] = counts.get(d_id, 0) + 1
            counts_by_type.setdefault(d_id, {})
            counts_by_type[d_id][task_type] = counts_by_type[d_id].get(task_type, 0) + 1
            active_tasks.setdefault(d_id, []).append(
                {
                    "id": str(task.id),
                    "status": task.status.value
                    if hasattr(task.status, "value")
                    else str(task.status),
                    "title": task.title or "",
                    "latest_status_message": (task.latest_status_message or "")[:200],
                }
            )

    return {
        "counts": counts,
        "counts_by_type": counts_by_type,
        "active_tasks": active_tasks,
    }


@router.patch("/projects/{project_id}/autonomous")
@limiter.limit("30/minute")
async def toggle_autonomous(
    request: Request,
    project_id: uuid.UUID,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    if "enabled" not in payload:
        raise HTTPException(status_code=400, detail="'enabled' field required")

    project.autonomous_enabled = bool(payload["enabled"])
    # Reset failure counter when re-enabling
    if project.autonomous_enabled:
        from app.shared_state import autonomous_failure_counts

        autonomous_failure_counts.pop(str(project_id), None)
    session.add(project)
    await session.commit()
    return {"autonomous_enabled": project.autonomous_enabled}


@router.get("/autonomous-status")
@limiter.limit("30/minute")
async def get_autonomous_status(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    from app.shared_state import autonomous_failure_counts, runtime_config

    projects = (
        (
            await session.execute(
                select(Project)
                .where(Project.id.in_(_user_project_ids(current_user["user_id"])))
                .order_by(Project.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    one_hour_ago = _utcnow() - timedelta(hours=1)

    # Count autonomous RESEARCH tasks per project in the last hour (matches rate limit guard)
    user_pids = _user_project_ids(current_user["user_id"])
    auto_tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id.in_(user_pids),
                    Task.created_by == "ORCHESTRATOR",
                    Task.created_at >= one_hour_ago,
                )
            )
        )
        .scalars()
        .all()
    )
    tasks_per_project: dict = {}
    for t in auto_tasks:
        if (t.first_payload).get("type") == "research":
            tasks_per_project[t.project_id] = tasks_per_project.get(t.project_id, 0) + 1

    # Batch query: most recent autonomous task per project
    last_research_subq = (
        select(
            Task.project_id,
            func.max(Task.created_at).label("max_created_at"),
        )
        .where(Task.created_by == "ORCHESTRATOR")
        .group_by(Task.project_id)
        .subquery()
    )
    last_research_rows = (
        await session.execute(
            select(last_research_subq.c.project_id, last_research_subq.c.max_created_at)
        )
    ).all()
    last_research_map = {
        row.project_id: row.max_created_at for row in last_research_rows
    }

    result = []
    for project in projects:
        project_id_str = str(project.id)
        result.append(
            {
                "project_id": project_id_str,
                "project_name": project.name,
                "autonomous_enabled": project.autonomous_enabled,
                "consecutive_failures": autonomous_failure_counts.get(
                    project_id_str, 0
                ),
                "last_research_at": last_research_map[project.id].isoformat()
                if last_research_map.get(project.id)
                else None,
                "tasks_spawned_last_hour": tasks_per_project.get(project.id, 0),
                "rate_limit": runtime_config["max_autonomous_tasks_per_hour"],
            }
        )

    return {"projects": result}
