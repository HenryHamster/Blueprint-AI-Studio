from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from typing import Optional, Dict, Any, List, TypedDict
import os
import re
import uuid
import json
import httpx
import logging
import asyncio
from collections import deque, defaultdict
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, update
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_session
from app.shared_state import limiter, circuit_breakers as _circuit_breakers
from app.models import Task, TaskStatus, TaskHistory, Project, ProjectMember, Artifact, ArtifactType, UserIntegration, AllowedUser, MaxPlanAccount, DeploymentState, DeploymentStatus, Alert, ProjectMessage, MessageType, _utcnow
from app.auth import get_current_user, require_admin

from app.github import generate_installation_access_token
from app.broadcast import broadcast_to_project

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(get_current_user)]
)

_ROLE_LEVEL = {"viewer": 0, "member": 1, "owner": 2}

def _user_project_ids(user_id: str):
    """Subquery returning project IDs the user is a member of (any role)."""
    return select(ProjectMember.project_id).where(ProjectMember.user_id == user_id)

async def _get_user_project(project_id: uuid.UUID, user_id: str, session: AsyncSession) -> Project:
    """Fetch a project ensuring the user is a member. Raises 404 if not found."""
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    member = (await session.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    )).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Project not found")
    return project

async def _get_user_project_with_role(
    project_id: uuid.UUID, user_id: str, session: AsyncSession, min_role: str = "viewer"
) -> Project:
    """Fetch a project ensuring the user has at least min_role. Raises 403 if insufficient."""
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    member = (await session.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    )).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Project not found")
    if _ROLE_LEVEL.get(member.role, 0) < _ROLE_LEVEL[min_role]:
        raise HTTPException(status_code=403, detail=f"Requires {min_role} role on this project")
    return project

async def _get_user_task(task_id: uuid.UUID, user_id: str, session: AsyncSession) -> Task:
    """Fetch a task ensuring the user is a member of its project. Raises 404 if not found."""
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    member = (await session.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == task.project_id,
            ProjectMember.user_id == user_id,
        )
    )).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Task not found")
    return task

async def _get_user_task_with_role(
    task_id: uuid.UUID, user_id: str, session: AsyncSession, min_role: str = "member"
) -> Task:
    """Fetch a task ensuring the user has at least min_role on its project."""
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    member = (await session.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == task.project_id,
            ProjectMember.user_id == user_id,
        )
    )).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Task not found")
    if _ROLE_LEVEL.get(member.role, 0) < _ROLE_LEVEL[min_role]:
        raise HTTPException(status_code=403, detail=f"Requires {min_role} role on this project")
    return task

TERMINAL_STATUSES = [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]

@router.get("/tasks")
async def get_all_tasks(
    limit: int = Query(50, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    status: List[TaskStatus] = Query(default=[]),
    terminal_only: bool = Query(False),
    archived: Optional[bool] = Query(None),
    search: Optional[str] = Query(None, max_length=200),
    created_after: Optional[datetime] = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    # Build shared filter conditions
    filters = [Task.project_id.in_(_user_project_ids(current_user["user_id"]))]
    if status:
        filters.append(Task.status.in_(status))
    elif terminal_only:
        filters.append(Task.status.in_(TERMINAL_STATUSES))
    if archived is not None:
        filters.append(Task.archived == archived)
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        search_pattern = f"%{escaped}%"
        filters.append(
            (Task.title.ilike(search_pattern, escape="\\")) | (Task.description.ilike(search_pattern, escape="\\"))
        )
    if created_after:
        filters.append(Task.created_at >= created_after)

    count_query = select(func.count()).select_from(Task)
    for f in filters:
        count_query = count_query.where(f)
    total = (await session.execute(count_query)).scalar() or 0

    query = select(Task).order_by(Task.created_at.desc()).offset(offset).limit(limit)
    for f in filters:
        query = query.where(f)

    result = await session.execute(query)
    tasks = result.scalars().all()

    # Join artifacts to include all artifacts per task
    task_ids = [t.id for t in tasks]
    artifacts_map: Dict[str, List[Dict[str, Any]]] = {}
    pr_map: Dict[str, str] = {}
    logs_map: Dict[str, str] = {}
    logs_truncated_map: Dict[str, bool] = {}
    research_map: Dict[str, str] = {}
    commit_map: Dict[str, Dict[str, Any]] = {}
    if task_ids:
        artifact_stmt = select(Artifact).where(Artifact.task_id.in_(task_ids)).order_by(Artifact.created_at.asc())
        artifacts = (await session.execute(artifact_stmt)).scalars().all()
        for a in artifacts:
            tid = str(a.task_id)
            if tid not in artifacts_map:
                artifacts_map[tid] = []
            artifacts_map[tid].append({
                "artifact_type": a.artifact_type,
                "pr_url": a.pr_url,
                "commit_hash": a.commit_hash,
                "repo_url": a.repo_url,
                "iteration": a.iteration,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            })
            if a.artifact_type == ArtifactType.PULL_REQUEST and a.pr_url:
                pr_map[tid] = a.pr_url
            if a.artifact_type == ArtifactType.ERROR_LOG and a.logs:
                logs_map[tid] = a.logs[:2000]
                logs_truncated_map[tid] = len(a.logs) > 2000
            if a.artifact_type == ArtifactType.RESEARCH_FINDINGS and a.logs:
                try:
                    parsed = json.loads(a.logs[:100000])
                    if isinstance(parsed, list):
                        research_map[str(a.task_id)] = parsed
                    elif isinstance(parsed, dict):
                        logs = parsed.get('logs', [])
                        if isinstance(logs, list):
                            research_map[str(a.task_id)] = logs
                except (json.JSONDecodeError, AttributeError):
                    pass
            if a.artifact_type == ArtifactType.GIT_COMMIT and a.commit_hash:
                commit_map[tid] = {"commit_hash": a.commit_hash, "repo_url": a.repo_url}

    # Fetch task history (last 10 entries per task)
    history_map: Dict[str, List[Dict[str, Any]]] = {}
    if task_ids:
        history_stmt = (
            select(TaskHistory)
            .where(TaskHistory.task_id.in_(task_ids))
            .order_by(TaskHistory.timestamp.desc())
        )
        all_history = (await session.execute(history_stmt)).scalars().all()
        for h in all_history:
            tid = str(h.task_id)
            if tid not in history_map:
                history_map[tid] = []
            if len(history_map[tid]) < 10:
                history_map[tid].append({
                    "timestamp": h.timestamp.isoformat() if h.timestamp else None,
                    "action": h.action,
                    "details": h.details,
                })

    # Build parent→children map from the fetched result set (no extra DB query)
    children_map: Dict[str, List[str]] = {}
    for t in tasks:
        if t.parent_task_id:
            parent_key = str(t.parent_task_id)
            if parent_key not in children_map:
                children_map[parent_key] = []
            children_map[parent_key].append(str(t.id))

    # Return tasks with artifact data attached
    result_list = []
    for t in tasks:
        task_type = (t.first_payload).get("type", "create")
        task_dict = {
            "id": str(t.id),
            "project_id": str(t.project_id),
            "title": t.title,
            "description": t.description,
            "status": t.status,
            "task_type": task_type,
            "vm_instance_id": t.vm_instance_id,
            "latest_status_message": t.latest_status_message,
            "status_log": t.status_log or [],
            "created_by": t.created_by,
            "requires_approval": t.requires_approval,
            "parent_task_id": str(t.parent_task_id) if t.parent_task_id else None,
            "depends_on": t.depends_on or [],
            "plan_output": t.plan_output,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "last_heartbeat": t.last_heartbeat.isoformat() if t.last_heartbeat else None,
            "attempt_count": t.attempt_count,
            "payloads": t.payloads,
            "context": {k: v for k, v in (t.context or {}).items() if not k.startswith("max_plan_")},
            "archived": t.archived,
            "artifacts": artifacts_map.get(str(t.id), []),
            "pr_url": pr_map.get(str(t.id)),
            "error_logs": logs_map.get(str(t.id)),
            "error_logs_truncated": logs_truncated_map.get(str(t.id), False),
            "research_findings": research_map.get(str(t.id)),
            "commit_hash": commit_map.get(str(t.id), {}).get("commit_hash"),
            "repo_url": commit_map.get(str(t.id), {}).get("repo_url"),
            "history": history_map.get(str(t.id), []),
            "child_task_ids": children_map.get(str(t.id), []),
        }
        result_list.append(task_dict)

    # Global counts (not affected by pagination/filters)
    active_count = (await session.execute(
        select(func.count()).select_from(Task).where(
            Task.status.in_([TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING])
        )
    )).scalar() or 0
    evaluating_count = (await session.execute(
        select(func.count()).select_from(Task).where(Task.status == TaskStatus.EVALUATING)
    )).scalar() or 0
    queued_count = (await session.execute(
        select(func.count()).select_from(Task).where(Task.status == TaskStatus.QUEUED)
    )).scalar() or 0

    return {"tasks": result_list, "total": total, "active_count": active_count, "evaluating_count": evaluating_count, "queued_count": queued_count}

class BulkArchiveRequest(BaseModel):
    task_ids: Optional[List[str]] = None
    filter: Optional[str] = None  # 'completed' or 'terminal'

@router.post("/tasks/bulk-archive")
async def bulk_archive_tasks(
    request: BulkArchiveRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    user_project_ids = _user_project_ids(current_user["user_id"])
    if request.task_ids is not None:
        archived = []
        for raw_id in request.task_ids:
            try:
                task = await session.get(Task, uuid.UUID(raw_id))
            except (ValueError, AttributeError):
                continue
            if task and task.status in TERMINAL_STATUSES and not task.archived:
                # Verify task belongs to user's project (via membership)
                member = (await session.execute(
                    select(ProjectMember).where(ProjectMember.project_id == task.project_id, ProjectMember.user_id == current_user["user_id"])
                )).scalar_one_or_none()
                if not member:
                    continue
                task.archived = True
                session.add(task)
                archived.append(task)
    elif request.filter in ('completed', 'terminal'):
        statuses = [TaskStatus.COMPLETED] if request.filter == 'completed' else TERMINAL_STATUSES
        query = select(Task).where(Task.archived.is_(False), Task.status.in_(statuses), Task.project_id.in_(user_project_ids))
        result = await session.execute(query)
        archived = result.scalars().all()
        for task in archived:
            task.archived = True
            session.add(task)
    else:
        raise HTTPException(status_code=400, detail="Must provide task_ids or a valid filter ('completed' or 'terminal')")

    await session.commit()
    return {"archived_count": len(archived)}

class BulkActionRequest(BaseModel):
    action: str  # 'approve' or 'retry'
    task_ids: List[str]

@router.post("/tasks/bulk-action")
@limiter.limit("60/minute")
async def bulk_action_tasks(
    request: Request,
    body: BulkActionRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    if body.action not in ('approve', 'retry'):
        raise HTTPException(status_code=400, detail="action must be 'approve' or 'retry'")

    results = []

    for raw_id in body.task_ids:
        try:
            task_uuid = uuid.UUID(raw_id)
        except (ValueError, AttributeError):
            results.append({"task_id": raw_id, "status": "error", "error": "Invalid task ID"})
            continue

        try:
            task = await _get_user_task_with_role(task_uuid, current_user["user_id"], session, "member")
        except HTTPException as e:
            results.append({"task_id": raw_id, "status": "error", "error": e.detail})
            continue

        try:
            if body.action == 'retry':
                task.status = TaskStatus.QUEUED
                task_type = (task.first_payload).get("type", "")
                is_parent_type = task_type in ("research", "plan")
                from app.task_lifecycle import _requeue_parent_task
                children = []
                if is_parent_type:
                    children = (await session.execute(
                        select(Task).where(Task.parent_task_id == task.id)
                    )).scalars().all()
                old_vm = None
                if children:
                    await _requeue_parent_task(task, session)
                else:
                    old_vm = task.vm_instance_id
                    task.attempt_count = 0
                    task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                    task.last_heartbeat = None
                history = TaskHistory(
                    task_id=task.id,
                    action="MODIFIED",
                    details={"changes": {"status": "QUEUED"}, "modified_by": "BULK_ACTION"}
                )
                session.add(task)
                session.add(history)
                await session.commit()

                # Delete old VM after commit, same as modify_task
                if old_vm:
                    from app.main import _ensure_vm_deleted
                    await _ensure_vm_deleted(old_vm, reason="admin_requeue")

            elif body.action == 'approve':
                was_blocked = task.requires_approval
                task.requires_approval = False

                if was_blocked:
                    logger.info(f"[ADMIN] Task {task.id} bulk-approved (was blocked: {task.latest_status_message or 'unknown reason'})")

                if was_blocked and task.status in (TaskStatus.COMPLETED, TaskStatus.EVALUATING):
                    ctx = task.context or {}
                    rollup_pr_number = ctx.get("rollup_pr_number")

                    if rollup_pr_number:
                        # Rollup PR approval: merge the rollup PR directly
                        try:
                            from app.main import get_github_token_for_task, _runtime_config
                            from app.task_lifecycle import _cleanup_branches
                            from app.github import merge_pull_request, create_pr_comment
                            gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                            merge_result = await merge_pull_request(gh_token, owner, repo, rollup_pr_number)
                            if merge_result.get("merged"):
                                task.status = TaskStatus.COMPLETED
                                task.context = {**ctx, "pr_merged": True}
                                task.latest_status_message = f"Human-approved merge of rollup PR #{rollup_pr_number}"
                                await create_pr_comment(gh_token, owner, repo, rollup_pr_number,
                                    "**Blueprint Review Pipeline**: Human-approved rollup merge.")
                                logger.info(f"[ADMIN] Bulk-approved rollup merge of PR #{rollup_pr_number}")
                                session.add(task)
                                await session.commit()
                                await _cleanup_branches(task, session)
                                base_branch = (task.first_payload).get("base_branch", "main")
                                if base_branch == "main":
                                    try:
                                        type_model = _runtime_config.get("task_type_models", {}).get("smoketest") or None
                                        type_effort = _runtime_config.get("task_type_efforts", {}).get("smoketest") or None
                                        health_check_urls = _runtime_config.get("health_check_urls") or []
                                        smoketest_task = Task(
                                            project_id=task.project_id,
                                            title=f"Smoketest after rollup merge to {base_branch}",
                                            description=f"Post-merge health checks for {owner}/{repo}@{base_branch}",
                                            payloads=[{
                                                "type": "smoketest",
                                                "base_branch": base_branch,
                                                **({"model": type_model} if type_model else {}),
                                                **({"effort": type_effort} if type_effort else {}),
                                                **({"health_check_urls": health_check_urls} if health_check_urls else {}),
                                            }],
                                            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                                            status=TaskStatus.QUEUED,
                                            created_by="ORCHESTRATOR",
                                        )
                                        session.add(smoketest_task)
                                        await session.commit()
                                        await session.refresh(smoketest_task)
                                        history_st = TaskHistory(
                                            task_id=smoketest_task.id, action="SPAWNED",
                                            details={"spawned_by": "ADMIN_BULK_ROLLUP_MERGE", "base_branch": base_branch}
                                        )
                                        session.add(history_st)
                                        await session.commit()
                                        await broadcast_to_project(
                                            str(task.project_id), "task_updated",
                                            {"task_id": str(smoketest_task.id), "status": TaskStatus.QUEUED}
                                        )
                                        logger.info(f"[SMOKETEST] Spawned smoketest task {smoketest_task.id} after bulk rollup merge to {base_branch}")
                                    except Exception as e:
                                        logger.warning(f"[SMOKETEST] Failed to spawn smoketest after bulk rollup merge: {e}", exc_info=True)
                            else:
                                task.latest_status_message = f"Approved but rollup merge failed: {merge_result.get('message', 'unknown')}"
                                logger.error(f"[ADMIN] Bulk-approved rollup merge of PR #{rollup_pr_number} failed: {merge_result.get('message')}")
                        except Exception as e:
                            task.latest_status_message = f"Approved but rollup merge failed: {str(e)[:200]}"
                            logger.error(f"[ADMIN] Bulk-approved rollup merge failed: {e}", exc_info=True)
                    else:
                        artifact = (await session.execute(
                            select(Artifact).where(Artifact.task_id == task.id,
                                                   Artifact.artifact_type == ArtifactType.PULL_REQUEST)
                        )).scalars().first()
                        if artifact and artifact.pr_url:
                            pr_number = int(artifact.pr_url.rstrip("/").split("/")[-1])
                            branch = f"blueprint/task-{str(task.id).replace('-','')[:8]}"
                            task_payload = task.first_payload
                            base_branch = task_payload.get("base_branch", "main")

                            if ctx.get("touched_critical"):
                                try:
                                    from app.main import get_github_token_for_task
                                    from app.github import merge_pull_request, create_pr_comment, delete_branch
                                    gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                                    merge_result = await merge_pull_request(gh_token, owner, repo, pr_number)
                                    if merge_result.get("merged"):
                                        task.status = TaskStatus.COMPLETED
                                        task.context = {**ctx, "pr_merged": True}
                                        task.latest_status_message = f"Human-approved merge of PR #{pr_number}"
                                        await create_pr_comment(gh_token, owner, repo, pr_number,
                                            "**Blueprint Review Pipeline**: Human-approved merge (security-critical files).")
                                        try:
                                            await delete_branch(gh_token, owner, repo, branch)
                                        except Exception:
                                            pass
                                        logger.info(f"[ADMIN] Bulk-approved merge of PR #{pr_number} (critical files)")
                                    else:
                                        task.latest_status_message = f"Approved but merge failed: {merge_result.get('message', 'unknown')}"
                                        logger.error(f"[ADMIN] Bulk-approved merge of PR #{pr_number} failed: {merge_result.get('message')}")
                                except Exception as e:
                                    task.latest_status_message = f"Approved but merge failed: {str(e)[:200]}"
                                    logger.error(f"[ADMIN] Bulk-approved merge failed: {e}", exc_info=True)
                            else:
                                from app.task_lifecycle import _spawn_pipeline_task
                                original_prompt = task_payload.get("prompt", task.description or "")
                                review_task = await _spawn_pipeline_task(
                                    task, "review", session, pr_number, branch, base_branch,
                                    original_prompt=original_prompt, original_task_id=str(task.id)
                                )
                                if not review_task:
                                    task.requires_approval = True
                                    task.status = TaskStatus.FAILED
                                    task.latest_status_message = "Iteration cap reached — no follow-on task spawned"
                                    session.add(task)
                                    await session.commit()
                                    results.append({"task_id": raw_id, "status": "error", "error": "Iteration cap reached"})
                                    continue

                history = TaskHistory(
                    task_id=task.id,
                    action="MODIFIED",
                    details={"changes": {"requires_approval": False}, "modified_by": "BULK_ACTION"}
                )
                session.add(task)
                session.add(history)
                await session.commit()

            results.append({"task_id": raw_id, "status": "ok"})
        except HTTPException as e:
            await session.rollback()
            results.append({"task_id": raw_id, "status": "error", "error": e.detail})
        except Exception as e:
            await session.rollback()
            logger.error(f"[ADMIN] Bulk action {body.action} failed for task {raw_id}: {e}", exc_info=True)
            results.append({"task_id": raw_id, "status": "error", "error": str(e)[:200]})

    success_count = sum(1 for r in results if r["status"] == "ok")
    error_count = len(results) - success_count
    return {"results": results, "success_count": success_count, "error_count": error_count}

@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    try:
        tid = uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Task not found")

    task = await _get_user_task(tid, current_user["user_id"], session)

    # Fetch all artifacts for this task (all types, not just last per type)
    artifact_stmt = select(Artifact).where(Artifact.task_id == tid).order_by(Artifact.created_at.asc())
    artifacts = (await session.execute(artifact_stmt)).scalars().all()
    artifacts_list = []
    pr_url = None
    error_logs = None
    error_logs_truncated = False
    research_findings = None
    commit_hash = None
    repo_url = None
    for a in artifacts:
        artifacts_list.append({
            "artifact_type": a.artifact_type,
            "pr_url": a.pr_url,
            "commit_hash": a.commit_hash,
            "repo_url": a.repo_url,
            "iteration": a.iteration,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        })
        if a.artifact_type == ArtifactType.PULL_REQUEST and a.pr_url:
            pr_url = a.pr_url
        if a.artifact_type == ArtifactType.ERROR_LOG and a.logs:
            error_logs_truncated = len(a.logs) > 2000
            error_logs = a.logs[:2000]
        if a.artifact_type == ArtifactType.RESEARCH_FINDINGS and a.logs:
            research_findings = a.logs[:100000]
        if a.artifact_type == ArtifactType.GIT_COMMIT and a.commit_hash:
            commit_hash = a.commit_hash
            repo_url = a.repo_url

    # Fetch full task history (all entries, not capped at 10)
    history_stmt = (
        select(TaskHistory)
        .where(TaskHistory.task_id == tid)
        .order_by(TaskHistory.timestamp.desc())
    )
    all_history = (await session.execute(history_stmt)).scalars().all()
    history = [
        {
            "timestamp": h.timestamp.isoformat() if h.timestamp else None,
            "action": h.action,
            "details": h.details,
        }
        for h in all_history
    ]

    # Fetch child task IDs
    child_ids_result = await session.execute(
        select(Task.id).where(Task.parent_task_id == tid)
    )
    child_task_ids = [str(row) for row in child_ids_result.scalars().all()]

    task_type = (task.first_payload).get("type", "create")
    return {
        "id": str(task.id),
        "project_id": str(task.project_id),
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "task_type": task_type,
        "vm_instance_id": task.vm_instance_id,
        "latest_status_message": task.latest_status_message,
        "status_log": task.status_log or [],
        "created_by": task.created_by,
        "requires_approval": task.requires_approval,
        "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
        "depends_on": task.depends_on or [],
        "plan_output": task.plan_output,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "last_heartbeat": task.last_heartbeat.isoformat() if task.last_heartbeat else None,
        "attempt_count": task.attempt_count,
        "payloads": task.payloads,
        "context": {k: v for k, v in (task.context or {}).items() if not k.startswith("max_plan_")},
        "archived": task.archived,
        "artifacts": artifacts_list,
        "pr_url": pr_url,
        "error_logs": error_logs,
        "error_logs_truncated": error_logs_truncated,
        "commit_hash": commit_hash,
        "repo_url": repo_url,
        "research_findings": research_findings,
        "history": history,
        "child_task_ids": child_task_ids,
    }

class CreateTaskPayload(BaseModel):
    github_repo: str = Field(..., pattern=r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")
    title: str = Field(..., min_length=1, max_length=200)
    prompt: str = Field(..., min_length=1, max_length=50000)
    base_branch: str = Field(default="main", max_length=255, pattern=r"^[a-zA-Z0-9_./-]*[a-zA-Z0-9_.-]$")
    task_type: str = Field(default="create", pattern=r"^(create|plan|research|review|fix|security_audit|qa|deps|docs)$")
    pr_number: Optional[int] = None
    branch: Optional[str] = Field(default=None, max_length=255, pattern=r"^[a-zA-Z0-9_./-]*[a-zA-Z0-9_.-]$")
    model: Optional[str] = Field(default=None, min_length=1)
    effort: Optional[str] = Field(default=None, pattern=r"^(low|medium|high)$")

    @field_validator("base_branch", "branch")
    @classmethod
    def validate_branch_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        for part in v.split("/"):
            if part in (".git", ".lock"):
                raise ValueError(f"Branch name contains reserved component: {part}")
            if part.startswith("."):
                raise ValueError(f"Branch component cannot start with a dot: {part}")
            if ".." in part:
                raise ValueError(f"Branch component cannot contain '..': {part}")
        return v

@router.post("/tasks")
@limiter.limit("60/minute")
async def create_task_manually(
    request: Request,
    payload: CreateTaskPayload,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    Create a Claude Code task. Auto-creates a project for the repo if one doesn't exist.
    """
    # Parse owner/repo
    github_owner, github_repo = payload.github_repo.split("/", 1)

    # Find project for this repo where user is a member (or create new)
    stmt = select(Project).where(
        Project.github_owner == github_owner,
        Project.github_repo == github_repo,
        Project.id.in_(_user_project_ids(current_user["user_id"])),
    )
    project = (await session.execute(stmt)).scalars().first()

    if not project:
        project = Project(
            name=payload.github_repo,
            user_id=current_user.get("user_id", "admin"),
            github_owner=github_owner,
            github_repo=github_repo,
        )
        session.add(project)
        await session.commit()
        await session.refresh(project)
        # Auto-add owner membership
        session.add(ProjectMember(project_id=project.id, user_id=current_user["user_id"], role="owner"))
        await session.commit()

    # Auto-generate VM instance ID so orchestrator worker picks up the task
    vm_name = f"agent-vm-{uuid.uuid4().hex}"

    # Build payload based on task type
    task_payload = {
        "type": payload.task_type,
        "prompt": payload.prompt,
        "base_branch": payload.base_branch,
    }
    if payload.model is not None:
        task_payload["model"] = payload.model
    if payload.effort is not None:
        task_payload["effort"] = payload.effort
    # Review/fix tasks need pr_number and branch
    if payload.task_type in ("review", "fix") and payload.pr_number:
        task_payload["pr_number"] = payload.pr_number
        task_payload["branch"] = payload.branch or ""
        if payload.task_type == "review":
            task_payload["original_prompt"] = payload.prompt
    # Research tasks auto-include project directions
    if payload.task_type == "research":
        active_directions = [d for d in (project.directions or []) if d.get("status") == "active"]
        task_payload["directions"] = active_directions

    # Guard: cap active tasks per project to prevent queue explosion
    active_count = (await session.execute(
        select(func.count(Task.id)).where(
            Task.project_id == project.id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING])
        )
    )).scalar() or 0
    if active_count >= 200:
        raise HTTPException(status_code=429, detail=f"Project has {active_count} active tasks (limit: 200)")

    new_task = Task(
        project_id=project.id,
        title=payload.title,
        description=payload.prompt,
        payloads=[task_payload],
        vm_instance_id=vm_name,
        status=TaskStatus.QUEUED,
        created_by="USER",
        launched_by_user_id=current_user["user_id"],
        requires_approval=False,
    )
    session.add(new_task)
    await session.commit()
    await session.refresh(new_task)

    # Log history
    history = TaskHistory(
        task_id=new_task.id,
        action="CREATED",
        details={
            "created_by": "USER",
            "method": "admin_launcher",
            "github_repo": payload.github_repo,
            "base_branch": payload.base_branch,
        }
    )
    session.add(history)
    await session.commit()

    return {
        "id": str(new_task.id),
        "project_id": str(project.id),
        "title": new_task.title,
        "status": new_task.status,
        "vm_instance_id": new_task.vm_instance_id,
    }

class RequestChangesPayload(BaseModel):
    feedback: str = Field(..., min_length=1, max_length=5000)
    action: str = Field(default="fix", pattern=r"^(fix|review)$")

@router.post("/tasks/{task_id}/request-changes")
async def request_changes(
    task_id: uuid.UUID,
    payload: RequestChangesPayload,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Request changes on a task awaiting approval. Spawns a fix or re-review task."""
    task = await _get_user_task_with_role(task_id, current_user["user_id"], session, "member")
    if not task.requires_approval:
        raise HTTPException(status_code=400, detail="Task is not awaiting approval")

    # Find the PR
    artifact = (await session.execute(
        select(Artifact).where(Artifact.task_id == task_id, Artifact.artifact_type == ArtifactType.PULL_REQUEST)
    )).scalars().first()
    if not artifact or not artifact.pr_url:
        raise HTTPException(status_code=400, detail="No PR found for this task")

    pr_number = int(artifact.pr_url.rstrip("/").split("/")[-1])
    branch = f"blueprint/task-{str(task.id).replace('-','')[:8]}"
    task_payload = task.first_payload
    base_branch = task_payload.get("base_branch", "main")

    from app.task_lifecycle import _spawn_pipeline_task
    if payload.action == "fix":  # noqa: SIM108 — two distinct multi-line async calls, ternary unreadable
        spawned = await _spawn_pipeline_task(
            task, "fix", session, pr_number, branch, base_branch,
            fix_prompt=f"Human reviewer requested changes:\n\n{payload.feedback}",
            original_task_id=str(task.id)
        )
    else:
        spawned = await _spawn_pipeline_task(
            task, "review", session, pr_number, branch, base_branch,
            original_prompt=task_payload.get("prompt", task.description or ""),
            original_task_id=str(task.id)
        )

    if spawned:
        task.requires_approval = False
        task.latest_status_message = f"Changes requested — spawned {payload.action} task"
        session.add(task)
        await session.commit()
        return {"spawned_task_id": str(spawned.id), "action": payload.action}

    raise HTTPException(status_code=500, detail="Failed to spawn task (iteration cap?)")

@router.post("/tasks/{task_id}/archive")
async def archive_task(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    task = await _get_user_task_with_role(task_id, current_user["user_id"], session, "member")
    if task.status not in TERMINAL_STATUSES:
        raise HTTPException(status_code=400, detail=f"Cannot archive task with status '{task.status}'. Only terminal tasks (COMPLETED, FAILED, CANCELLED) can be archived.")
    task.archived = True
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task

@router.post("/tasks/{task_id}/unarchive")
async def unarchive_task(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    task = await _get_user_task_with_role(task_id, current_user["user_id"], session, "member")
    task.archived = False
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task

@router.patch("/tasks/{task_id}")
@limiter.limit("60/minute")
async def modify_task(
    request: Request,
    task_id: uuid.UUID,
    payload: Dict[str, Any],
    background_tasks: BackgroundTasks,
    force_kill: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    task = await _get_user_task_with_role(task_id, current_user["user_id"], session, "member")

    _TERMINAL_STATUSES = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}

    changes = {}
    if "status" in payload:
        try:
            new_status = TaskStatus(payload["status"])

            # State machine validation: admins can always requeue or cancel.
            # Only block nonsensical transitions like FAILED → IN_PROGRESS.
            admin_allowed = {TaskStatus.QUEUED, TaskStatus.CANCELLED}
            if new_status not in admin_allowed and task.status in _TERMINAL_STATUSES:
                raise HTTPException(status_code=400, detail=f"Cannot transition from terminal state {task.status.value} to {new_status.value} (only requeue or cancel is allowed)")

            task.status = new_status
            changes["status"] = new_status.value

            # Clear approval flag on terminal statuses
            if new_status in (TaskStatus.CANCELLED, TaskStatus.FAILED):
                task.requires_approval = False

            # Delete VM and release account when moving to terminal state
            if new_status in (TaskStatus.CANCELLED, TaskStatus.FAILED) and task.vm_instance_id:
                from app.main import _ensure_vm_deleted
                if not force_kill:
                    # Graceful: commit terminal status first so agent's next heartbeat
                    # returns 4xx, triggering graceful shutdown (5s wind-down)
                    session.add(task)
                    await session.commit()
                    await asyncio.sleep(5)
                await _ensure_vm_deleted(task, reason="admin_terminal")
                acct_id = (task.context or {}).get("max_plan_account_id")
                if acct_id:
                    from app.max_plan_pool import release_max_plan_account
                    await release_max_plan_account(acct_id)

            # For low-priority tasks cancelled by user, store suppress_until so the
            # orchestrator can skip re-proposals for 180 days without expanding the dedup window
            if new_status == TaskStatus.CANCELLED and task.context and task.context.get("finding_hash"):
                task_priority = task.priority if task.priority is not None else 50
                if task_priority == 0:  # low priority tasks map to 0 in TASK_PRIORITY_MAP
                    from app.models import _utcnow
                    suppress_until = (_utcnow() + timedelta(days=180)).isoformat()
                    task.context = {**task.context, "suppress_until": suppress_until}
                    flag_modified(task, "context")
                    logger.info(f"[ADMIN] Set suppress_until={suppress_until} on cancelled low-priority task {task.id}")

            # Cascade cancellation to active children
            if new_status == TaskStatus.CANCELLED:
                from app.main import _ensure_vm_deleted as _cancel_vm
                from app.max_plan_pool import release_max_plan_account as _cancel_release
                active_children = (await session.execute(
                    select(Task).where(
                        Task.parent_task_id == task.id,
                        Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING])
                    )
                )).scalars().all()
                for child in active_children:
                    child.status = TaskStatus.CANCELLED
                    child.requires_approval = False
                    if child.vm_instance_id:
                        await _cancel_vm(child, reason="parent_cancelled")
                    child_acct = (child.context or {}).get("max_plan_account_id")
                    if child_acct:
                        _cancel_release(child_acct)
                    session.add(child)
                if active_children:
                    logger.info(f"[ADMIN] Cascaded CANCELLED to {len(active_children)} children of task {task.id}")

            # Reset retry state when manually re-queuing a task
            if new_status == TaskStatus.QUEUED:
                # Check if this is a research/plan parent with work children — re-evaluate
                # instead of re-run. Don't treat review/fix/create tasks as parents just
                # because they have incidental debug children.
                task_type = (task.first_payload).get("type", "")
                is_parent_type = task_type in ("research", "plan")
                from app.task_lifecycle import _requeue_parent_task
                children = []
                if is_parent_type:
                    children = (await session.execute(
                        select(Task).where(Task.parent_task_id == task.id)
                    )).scalars().all()
                if children:
                    retried = await _requeue_parent_task(task, session)
                    changes["status"] = TaskStatus.EVALUATING
                    changes["retried_children"] = retried
                else:
                    old_vm_id = task.vm_instance_id
                    task.attempt_count = 0
                    task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                    task.last_heartbeat = None
                    task.started_at = None
                    # Reset all retry counters for a clean slate
                    requeue_ctx = dict(task.context or {})
                    requeue_ctx.pop("provision_failures", None)
                    requeue_ctx.pop("retry_after", None)
                    requeue_ctx.pop("max_plan_account_id", None)
                    requeue_ctx.pop("blacklisted_accounts", None)
                    requeue_ctx.pop("_slot_released", None)
                    requeue_ctx.pop("_api_failure_retries", None)
                    task.context = requeue_ctx
                    changes["attempt_count"] = 0
                    changes["vm_instance_id"] = task.vm_instance_id
                    changes["_old_vm_to_delete"] = old_vm_id
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid status")

    if "requires_approval" in payload:
        was_blocked = task.requires_approval
        task.requires_approval = bool(payload["requires_approval"])
        changes["requires_approval"] = task.requires_approval

        if was_blocked and not task.requires_approval:
            logger.info(f"[ADMIN] Task {task.id} manually approved (was blocked: {task.latest_status_message or 'unknown reason'})")

        # Approval flow: if unblocking a completed task with a PR, continue pipeline
        if was_blocked and not task.requires_approval and task.status in (TaskStatus.COMPLETED, TaskStatus.EVALUATING):
            ctx = task.context or {}
            rollup_pr_number = ctx.get("rollup_pr_number")

            if rollup_pr_number:
                # Rollup PR approval: merge the rollup PR directly
                try:
                    from app.main import get_github_token_for_task, _runtime_config
                    from app.task_lifecycle import _cleanup_branches
                    from app.github import merge_pull_request, create_pr_comment
                    gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                    merge_result = await merge_pull_request(gh_token, owner, repo, rollup_pr_number)
                    if merge_result.get("merged"):
                        task.status = TaskStatus.COMPLETED
                        task.context = {**ctx, "pr_merged": True}
                        task.latest_status_message = f"Human-approved merge of rollup PR #{rollup_pr_number}"
                        await create_pr_comment(gh_token, owner, repo, rollup_pr_number,
                            "**Blueprint Review Pipeline**: Human-approved rollup merge.")
                        changes["merged_pr"] = rollup_pr_number
                        logger.info(f"[ADMIN] Human-approved rollup merge of PR #{rollup_pr_number}")
                        session.add(task)
                        await session.commit()
                        # Post-merge cleanup: branches + smoketest (mirrors webhook merge path)
                        await _cleanup_branches(task, session)
                        base_branch = (task.first_payload).get("base_branch", "main")
                        if base_branch == "main":
                            try:
                                type_model = _runtime_config.get("task_type_models", {}).get("smoketest") or None
                                type_effort = _runtime_config.get("task_type_efforts", {}).get("smoketest") or None
                                health_check_urls = _runtime_config.get("health_check_urls") or []
                                smoketest_task = Task(
                                    project_id=task.project_id,
                                    title=f"Smoketest after rollup merge to {base_branch}",
                                    description=f"Post-merge health checks for {owner}/{repo}@{base_branch}",
                                    payloads=[{
                                        "type": "smoketest",
                                        "base_branch": base_branch,
                                        **({"model": type_model} if type_model else {}),
                                        **({"effort": type_effort} if type_effort else {}),
                                        **({"health_check_urls": health_check_urls} if health_check_urls else {}),
                                    }],
                                    vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                                    status=TaskStatus.QUEUED,
                                    created_by="ORCHESTRATOR",
                                )
                                session.add(smoketest_task)
                                await session.commit()
                                await session.refresh(smoketest_task)
                                history = TaskHistory(
                                    task_id=smoketest_task.id, action="SPAWNED",
                                    details={"spawned_by": "ADMIN_ROLLUP_MERGE", "base_branch": base_branch}
                                )
                                session.add(history)
                                await session.commit()
                                await broadcast_to_project(
                                    str(task.project_id), "task_updated",
                                    {"task_id": str(smoketest_task.id), "status": TaskStatus.QUEUED}
                                )
                                logger.info(f"[SMOKETEST] Spawned smoketest task {smoketest_task.id} after admin rollup merge to {base_branch}")
                            except Exception as e:
                                logger.warning(f"[SMOKETEST] Failed to spawn smoketest after admin rollup merge: {e}", exc_info=True)
                    else:
                        changes["merge_error"] = merge_result.get("message", "unknown")
                        task.latest_status_message = f"Approved but rollup merge failed: {merge_result.get('message', 'unknown')}"
                        logger.error(f"[ADMIN] Approved rollup merge of PR #{rollup_pr_number} failed: {merge_result.get('message')}")
                except Exception as e:
                    changes["merge_error"] = str(e)[:200]
                    task.latest_status_message = f"Approved but rollup merge failed: {str(e)[:200]}"
                    logger.error(f"[ADMIN] Approved rollup merge failed: {e}", exc_info=True)
            else:
                artifact = (await session.execute(
                    select(Artifact).where(Artifact.task_id == task.id,
                                           Artifact.artifact_type == ArtifactType.PULL_REQUEST)
                )).scalars().first()
                if artifact and artifact.pr_url:
                    pr_number = int(artifact.pr_url.rstrip("/").split("/")[-1])
                    branch = f"blueprint/task-{str(task.id).replace('-','')[:8]}"
                    task_payload = task.first_payload
                    base_branch = task_payload.get("base_branch", "main")

                    if ctx.get("touched_critical"):
                        # Critical file approval: review already passed, merge the PR
                        try:
                            from app.main import get_github_token_for_task
                            from app.github import merge_pull_request, create_pr_comment, delete_branch
                            gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                            merge_result = await merge_pull_request(gh_token, owner, repo, pr_number)
                            if merge_result.get("merged"):
                                task.status = TaskStatus.COMPLETED
                                task.context = {**ctx, "pr_merged": True}
                                task.latest_status_message = f"Human-approved merge of PR #{pr_number}"
                                await create_pr_comment(gh_token, owner, repo, pr_number,
                                    "**Blueprint Review Pipeline**: Human-approved merge (security-critical files).")
                                try:
                                    await delete_branch(gh_token, owner, repo, branch)
                                except Exception:
                                    pass
                                changes["merged_pr"] = pr_number
                                logger.info(f"[ADMIN] Human-approved merge of PR #{pr_number} (critical files)")
                            else:
                                changes["merge_error"] = merge_result.get("message", "unknown")
                                task.latest_status_message = f"Approved but merge failed: {merge_result.get('message', 'unknown')}"
                                logger.error(f"[ADMIN] Approved merge of PR #{pr_number} failed: {merge_result.get('message')}")
                        except Exception as e:
                            changes["merge_error"] = str(e)[:200]
                            task.latest_status_message = f"Approved but merge failed: {str(e)[:200]}"
                            logger.error(f"[ADMIN] Approved merge failed: {e}", exc_info=True)
                    else:
                        # Security scan approval: review hasn't happened yet, spawn it
                        from app.task_lifecycle import _spawn_pipeline_task
                        original_prompt = task_payload.get("prompt", task.description or "")
                        review_task = await _spawn_pipeline_task(
                            task, "review", session, pr_number, branch, base_branch,
                            original_prompt=original_prompt, original_task_id=str(task.id)
                        )
                        if review_task:
                            changes["spawned_review"] = str(review_task.id)
                        else:
                            # Iteration cap reached — revert requires_approval and return error
                            task.requires_approval = True
                            task.status = TaskStatus.FAILED
                            task.latest_status_message = "Iteration cap reached — no follow-on task spawned"
                            session.add(task)
                            await session.commit()
                            raise HTTPException(
                                status_code=422,
                                detail="Iteration cap reached — cannot spawn follow-on task. Archive this task or reset iteration count."
                            )
                    # Task stays COMPLETED regardless
                    if "status" in payload and TaskStatus(payload["status"]) == TaskStatus.QUEUED:
                        task.status = TaskStatus.COMPLETED
                        changes["status"] = TaskStatus.COMPLETED

    if "payloads" in payload:
        task.payloads = payload["payloads"]
        changes["payloads"] = "updated"

    if "parent_task_id" in payload:
        task.parent_task_id = uuid.UUID(payload["parent_task_id"]) if payload["parent_task_id"] else None
        changes["parent_task_id"] = str(task.parent_task_id)

    if "context" in payload and isinstance(payload["context"], dict):
        merged = {**(task.context or {}), **payload["context"]}
        if len(json.dumps(merged)) > 102400:
            raise HTTPException(status_code=400, detail="Merged context exceeds 100KB limit")
        task.context = merged
        changes["context"] = "updated"

    if "depends_on" in payload:
        new_deps = payload["depends_on"] if isinstance(payload["depends_on"], list) else []
        # Validate: all dependency UUIDs must exist and not create cycles
        if new_deps:
            try:
                dep_uuids = [uuid.UUID(d) for d in new_deps]
            except (ValueError, AttributeError):
                raise HTTPException(status_code=400, detail="Invalid UUID in depends_on")
            # Check all referenced tasks exist
            existing = (await session.execute(
                select(Task.id).where(Task.id.in_(dep_uuids))
            )).scalars().all()
            if len(existing) != len(dep_uuids):
                raise HTTPException(status_code=400, detail="One or more depends_on task IDs do not exist")
            # Cycle detection: walk depends_on graph from each dep to see if we reach task.id
            task_id_str = str(task.id)
            async def _has_path_to(start_id: str, target_id: str, visited: set, depth: int = 0) -> bool:
                if depth > 50:
                    return False  # DAG too deep, treat as no cycle
                if start_id == target_id:
                    return True
                if start_id in visited:
                    return False
                visited.add(start_id)
                dep_task = await session.get(Task, uuid.UUID(start_id))
                if not dep_task:
                    return False
                for d in (dep_task.depends_on or []):
                    if await _has_path_to(d, target_id, visited, depth + 1):
                        return True
                return False
            for dep_id in new_deps:
                if await _has_path_to(dep_id, task_id_str, set()):
                    raise HTTPException(status_code=400, detail=f"Circular dependency detected: {dep_id} eventually depends on {task_id_str}")
        task.depends_on = new_deps
        changes["depends_on"] = task.depends_on

    if changes:
        session.add(task)

        history = TaskHistory(
            task_id=task.id,
            action="MODIFIED",
            details={"changes": changes, "modified_by": "USER"}
        )
        session.add(history)
        await session.commit()
        await session.refresh(task)

        # Delete old VM if a requeue overwrote vm_instance_id
        old_vm = changes.pop("_old_vm_to_delete", None)
        if old_vm:
            from app.main import _ensure_vm_deleted
            await _ensure_vm_deleted(old_vm, reason="admin_requeue")

        # Trigger rollup check if child status/context changed
        from app.task_lifecycle import _check_and_trigger_rollup, _cleanup_branches
        should_check_rollup = False
        if "status" in payload and task.parent_task_id:
            new_status = TaskStatus(payload["status"])
            if new_status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                should_check_rollup = True
            # Clean up staging branches when parent task is cancelled/failed
            if new_status in (TaskStatus.CANCELLED, TaskStatus.FAILED):
                ctx = task.context or {}
                if ctx.get("plan_branch") or ctx.get("research_branch"):
                    await _cleanup_branches(task, session)
        if "context" in payload and task.parent_task_id and isinstance(payload["context"], dict) and payload["context"].get("pr_merged"):
            # pr_merged was set via admin → trigger rollup
            should_check_rollup = True
        if should_check_rollup:
            await _check_and_trigger_rollup(task, session)

    return {
        "id": str(task.id),
        "project_id": str(task.project_id),
        "title": task.title,
        "status": task.status,
        "vm_instance_id": task.vm_instance_id,
        "attempt_count": task.attempt_count,
        "requires_approval": task.requires_approval,
    }

@router.post("/projects/{project_id}/clear-wave-deps")
async def clear_wave_deps(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Clear depends_on on all QUEUED tasks in a project to unblock wave-gated tasks."""
    await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")
    result = await session.execute(
        select(Task).where(Task.project_id == project_id, Task.status == TaskStatus.QUEUED)
    )
    tasks = result.scalars().all()
    cleared = 0
    for task in tasks:
        if task.depends_on:
            task.depends_on = []
            session.add(task)
            cleared += 1
    await session.commit()
    return {"cleared": cleared, "total_queued": len(tasks)}

@router.post("/tasks/{task_id}/rollup")
async def trigger_rollup(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Manually trigger rollup PR for an EVALUATING parent task."""
    from app.main import get_github_token_for_task
    from app.task_lifecycle import _spawn_pipeline_task
    from app.github import create_pull_request, merge_pull_request, delete_branch

    task = await _get_user_task_with_role(task_id, current_user["user_id"], session, "member")
    if task.status != TaskStatus.EVALUATING:
        raise HTTPException(status_code=400, detail=f"Task is {task.status}, not EVALUATING")

    ctx = task.context or {}
    staging_branch = ctx.get("plan_branch") or ctx.get("research_branch")
    target_branch = ctx.get("parent_base") or ctx.get("original_base")

    if not staging_branch or not target_branch:
        raise HTTPException(status_code=400, detail="Task has no staging/target branch info in context")

    gh_token, owner, repo = await get_github_token_for_task(task.id, session)

    # Count children
    from sqlmodel import select
    children = (await session.execute(
        select(Task).where(Task.parent_task_id == task.id)
    )).scalars().all()

    pr_title = f"[Blueprint] Rollup: {task.title}"
    pr_body = f"Rollup of {len(children)} subtasks from `{staging_branch}`\n\n---\n*Automated by Blueprint Pipeline*"

    pr_result = await create_pull_request(gh_token, owner, repo,
        title=pr_title, head=staging_branch, base=target_branch, body=pr_body)
    rollup_pr_url = pr_result.get("html_url", "")
    rollup_pr_number = pr_result.get("number")

    is_target_intermediate = target_branch.startswith("research/") or target_branch.startswith("plan/")

    if is_target_intermediate:
        merge_result = await merge_pull_request(gh_token, owner, repo, rollup_pr_number)
        if merge_result.get("merged"):
            task.status = TaskStatus.COMPLETED
            task.context = {**ctx, "pr_merged": True, "rollup_pr_url": rollup_pr_url}
            task.latest_status_message = f"Rollup PR auto-merged into {target_branch}"
            await delete_branch(gh_token, owner, repo, staging_branch)
        else:
            task.latest_status_message = f"Rollup merge failed: {merge_result.get('message')}"
    else:
        task.context = {**ctx, "rollup_pr_url": rollup_pr_url, "rollup_pr_number": rollup_pr_number}
        review_task = await _spawn_pipeline_task(
            task, "review", session, rollup_pr_number, staging_branch, target_branch,
            original_prompt=task.description or task.title,
            original_task_id=str(task.id)
        )
        if review_task:
            task.latest_status_message = f"Rollup PR #{rollup_pr_number} created, review spawned → {review_task.id}"
        else:
            task.status = TaskStatus.FAILED
            task.latest_status_message = f"Rollup PR #{rollup_pr_number} created but review not spawned (iteration cap)"

    session.add(task)
    await session.commit()

    return {
        "rollup_pr_url": rollup_pr_url,
        "rollup_pr_number": rollup_pr_number,
        "target": target_branch,
        "status": task.status,
    }

# --- Deployment State & Health ---

def _get_zone_health_summary() -> dict:
    try:
        from app.compute import get_zone_health_summary
        return get_zone_health_summary()
    except Exception:
        return {"total_zones": 0, "available": 0, "exhausted": []}

async def build_deploy_state_payload(session: AsyncSession) -> dict:
    """Assemble the deploy state payload. Used by both HTTP endpoint and WS broadcasts."""
    import os
    from app.shared_state import deploy_monitoring, external_health, runtime_config
    import app.main as _main_module

    state = await session.get(DeploymentState, 1)
    if not state:
        return {"error": "No deployment state found — service may not have initialized yet"}

    # Calculate monitoring window progress
    window_progress = None
    if deploy_monitoring["window_start"]:
        duration = deploy_monitoring["extended_window"] if deploy_monitoring["is_extended"] else deploy_monitoring["window_duration"]
        elapsed = (_utcnow() - deploy_monitoring["window_start"]).total_seconds()
        window_progress = {"elapsed_seconds": int(elapsed), "total_seconds": duration, "percent": min(100, int(elapsed / duration * 100))}

    # Calculate recent failure rate
    results = deploy_monitoring["task_results"]
    failure_rate = 0.0
    if results:
        failures = sum(1 for _, outcome, _ in results if outcome == "failure")
        failure_rate = failures / len(results)

    return {
        "current_revision": state.current_revision,
        "current_sha": state.current_sha,
        "deployed_at": state.deployed_at.isoformat() if state.deployed_at else None,
        "status": state.status,
        "last_known_good_revision": state.last_known_good_revision,
        "last_known_good_sha": state.last_known_good_sha,
        "rolled_back_from_sha": state.rolled_back_from_sha,
        "rollback_reason": state.rollback_reason,
        "rollback_count": state.rollback_count,
        "pause_reason": state.pause_reason,
        "paused_at": state.paused_at.isoformat() if state.paused_at else None,
        "reason": state.pause_reason if state.status == DeploymentStatus.PAUSED else None,
        "pre_pause_max_vms": state.pre_pause_max_vms,
        "monitoring_window": window_progress,
        "failure_rate": round(failure_rate, 3),
        "task_results_count": len(results),
        "external_health": external_health,
        "max_concurrent_vms": runtime_config["max_concurrent_vms"],
        "zone_health": _get_zone_health_summary(),
        "gcp_project_id": os.getenv("GCP_PROJECT_ID", ""),
        "health_recovery_streak": getattr(_main_module, "_health_recovery_streak", 0),
        "health_recovery_streak_required": 3,
        "health_failure_streak": getattr(_main_module, "_pause_failure_streak", 0),
        "health_failure_streak_threshold": 3,
    }

@router.get("/activity")
async def get_activity(
    project_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    event_types: Optional[List[str]] = Query(None),
    since: Optional[datetime] = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Return a reverse-chronological list of recent system events."""
    seven_days_ago = _utcnow() - timedelta(days=7)
    cutoff = since if since is not None else seven_days_ago
    events: List[Dict[str, Any]] = []

    # --- 1 & 2: Completed and Failed tasks in last 7 days (or since cutoff) ---
    user_project_ids = _user_project_ids(current_user["user_id"])
    task_filters = [
        Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
        Task.created_at >= cutoff,
        Task.project_id.in_(user_project_ids),
    ]
    if project_id:
        task_filters.append(Task.project_id == project_id)

    tasks_result = await session.execute(
        select(Task).where(*task_filters)
    )
    recent_tasks = tasks_result.scalars().all()

    task_ids = [t.id for t in recent_tasks] if recent_tasks else []

    # Fetch relevant artifacts in one query
    artifact_map: Dict[str, Dict[str, Any]] = {}  # task_id -> artifact info
    research_artifact_map: Dict[str, Dict[str, Any]] = {}  # task_id -> research artifact
    error_log_map: Dict[str, str] = {}  # task_id -> truncated error logs
    investigation_report_map: Dict[str, str] = {}  # task_id -> summary text

    if task_ids:
        artifacts_result = await session.execute(
            select(Artifact)
            .where(
                Artifact.task_id.in_(task_ids),
                Artifact.artifact_type.in_([
                    ArtifactType.PULL_REQUEST,
                    ArtifactType.ERROR_LOG,
                    ArtifactType.RESEARCH_FINDINGS,
                    ArtifactType.INVESTIGATION_REPORT,
                ])
            )
            .order_by(Artifact.created_at.asc())
        )
        for a in artifacts_result.scalars().all():
            tid = str(a.task_id)
            if a.artifact_type == ArtifactType.PULL_REQUEST:
                artifact_map[tid] = {
                    "pr_url": a.pr_url,
                    "repo_url": a.repo_url,
                    "commit_hash": a.commit_hash[:7] if a.commit_hash else None,
                }
            elif a.artifact_type == ArtifactType.ERROR_LOG and a.logs and tid not in error_log_map:
                error_log_map[tid] = a.logs[:300]
            elif a.artifact_type == ArtifactType.RESEARCH_FINDINGS:
                research_artifact_map[tid] = {
                    "artifact_created_at": a.created_at,
                    "finding_count": _count_research_findings(a.logs),
                }
            elif a.artifact_type == ArtifactType.INVESTIGATION_REPORT and a.logs:
                investigation_report_map[tid] = a.logs[:2000]

    for t in recent_tasks:
        tid = str(t.id)
        task_type = (t.first_payload).get("type", "create")
        pr_info = artifact_map.get(tid, {})

        # Determine timestamp: last status_log entry or updated_at fallback
        completed_at = None
        if t.status_log:
            last_entry = t.status_log[-1]
            try:
                completed_at = datetime.fromisoformat(last_entry["t"])
            except (KeyError, ValueError):
                pass
        if completed_at is None:
            # fall back to created_at since Task has no updated_at column
            completed_at = t.created_at

        duration_seconds = None
        if completed_at is not None and t.started_at is not None:
            duration_seconds = (completed_at - t.started_at).total_seconds()

        if t.status == TaskStatus.COMPLETED:
            events.append({
                "id": f"task-{tid}",
                "type": "TASK_COMPLETED",
                "timestamp": completed_at.isoformat() if completed_at else None,
                "title": t.title or "Completed task",
                "task_id": tid,
                "task_type": task_type,
                "pr_url": pr_info.get("pr_url"),
                "repo_url": pr_info.get("repo_url"),
                "commit_hash": pr_info.get("commit_hash"),
                "error_summary": None,
                "finding_count": None,
                "deploy_sha": None,
                "deploy_status": None,
                "rollback_count": None,
                "duration_seconds": duration_seconds,
            })
        elif t.status == TaskStatus.FAILED:
            events.append({
                "id": f"task-{tid}",
                "type": "TASK_FAILED",
                "timestamp": completed_at.isoformat() if completed_at else None,
                "title": t.title or "Failed task",
                "task_id": tid,
                "task_type": task_type,
                "pr_url": None,
                "repo_url": None,
                "commit_hash": None,
                "error_summary": error_log_map.get(tid),
                "finding_count": None,
                "deploy_sha": None,
                "deploy_status": None,
                "rollback_count": None,
                "duration_seconds": duration_seconds,
            })

        # --- 3: Research findings events (one per task) ---
        if tid in research_artifact_map:
            ra = research_artifact_map[tid]
            ra_ts = ra["artifact_created_at"]
            events.append({
                "id": f"research-{tid}",
                "type": "RESEARCH_FINDINGS",
                "timestamp": ra_ts.isoformat() if ra_ts else None,
                "title": f"Research findings: {t.title or tid}",
                "task_id": tid,
                "task_type": task_type,
                "pr_url": None,
                "repo_url": None,
                "commit_hash": None,
                "error_summary": None,
                "finding_count": ra["finding_count"],
                "investigation_summary": investigation_report_map.get(tid),
                "deploy_sha": None,
                "deploy_status": None,
                "rollback_count": None,
            })

    # --- 3b: In-progress tasks (QUEUED / PROVISIONING / IN_PROGRESS) ---
    inprogress_filters = [
        Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
        Task.project_id.in_(user_project_ids),
    ]
    if project_id:
        inprogress_filters.append(Task.project_id == project_id)
    if since is not None:
        inprogress_filters.append(Task.created_at >= since)

    inprogress_result = await session.execute(
        select(Task).where(*inprogress_filters)
    )
    inprogress_tasks = inprogress_result.scalars().all()

    for t in inprogress_tasks:
        tid = str(t.id)
        task_type = (t.first_payload).get("type", "create")
        events.append({
            "id": f"inprogress-{tid}",
            "type": "TASK_IN_PROGRESS",
            "timestamp": t.started_at.isoformat() if t.started_at else t.created_at.isoformat(),
            "title": t.title or "In-progress task",
            "task_id": tid,
            "task_type": task_type,
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "latest_status_message": t.latest_status_message,
            "pr_url": None,
            "repo_url": None,
            "commit_hash": None,
            "error_summary": None,
            "finding_count": None,
            "deploy_sha": None,
            "deploy_status": None,
            "rollback_count": None,
        })

    # --- 4: Deploy event from DeploymentState singleton ---
    state = await session.get(DeploymentState, 1)
    if state and state.deployed_at:
        short_sha = state.current_sha[:7] if state.current_sha else None
        deploy_event: Dict[str, Any] = {
            "id": f"deploy-{short_sha or 'unknown'}",
            "type": "DEPLOY_EVENT",
            "timestamp": state.deployed_at.isoformat(),
            "title": f"Deploy {short_sha or 'unknown'} — {state.status}",
            "task_id": None,
            "task_type": None,
            "pr_url": None,
            "repo_url": None,
            "commit_hash": short_sha,
            "error_summary": None,
            "finding_count": None,
            "deploy_sha": short_sha,
            "deploy_status": str(state.status) if state.status else None,
            "rollback_count": state.rollback_count if (state.rollback_count or 0) > 0 else None,
        }
        if (state.rollback_count or 0) > 0 and state.rolled_back_from_sha:
            deploy_event["rolled_back_from_sha"] = state.rolled_back_from_sha[:7]
        events.append(deploy_event)

    # Sort descending by timestamp, nulls last
    events.sort(key=lambda e: e["timestamp"] or "", reverse=True)

    if event_types:
        events = [e for e in events if e["type"] in event_types]

    total = len(events)
    return {"events": events[:limit], "total": total}

def _count_research_findings(logs: Optional[str]) -> int:
    """Parse research findings artifact logs and return finding count."""
    if not logs:
        return 0
    try:
        parsed = json.loads(logs[:100000])
        if isinstance(parsed, list):
            return len(parsed)
        if isinstance(parsed, dict):
            inner = parsed.get("logs", [])
            if isinstance(inner, list):
                return len(inner)
    except (json.JSONDecodeError, AttributeError):
        pass
    return 0

# ── Zone Health (moved to app.routers.deploy) ───────────────────────────────


# ---------------------------------------------------------------------------
# Diagnostics helpers

# GET /api/admin/projects/{project_id}/diagnostics

# GET /api/admin/diagnostics  (aggregated across all projects)

async def compute_pipeline_health_summary(session: AsyncSession, project_id: uuid.UUID) -> Dict[str, Any]:
    """Compute lightweight pipeline health metrics for the last 24 hours.

    Returns a dict with:
    - success_rate: float or None
    - trend: 'improving' | 'stable' | 'declining' | None
    - failure_distribution: {failure_class: count} top 3
    - direction_stats: [{direction_id, merge_rate, cycles, possibly_saturated}]
    """
    from app.main import classify_failure as _classify_failure

    _now = _utcnow()
    window_start = _now - timedelta(hours=24)
    prev_window_start = window_start - timedelta(hours=24)

    def _derive_completed_at(t: Task):
        if t.status_log:
            try:
                return datetime.fromisoformat(t.status_log[-1]["t"])
            except (KeyError, ValueError):
                pass
        return t.created_at

    # Query tasks from extended 48h window to compute both current and previous period
    tasks_result = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.created_at >= prev_window_start,
        )
    )
    all_tasks = tasks_result.scalars().all()

    # Post-filter by completion timestamp
    completed_tasks = [
        t for t in all_tasks
        if t.status == TaskStatus.COMPLETED and _derive_completed_at(t) >= window_start
    ]
    failed_tasks = [
        t for t in all_tasks
        if t.status == TaskStatus.FAILED and _derive_completed_at(t) >= window_start
    ]

    total_terminal = len(completed_tasks) + len(failed_tasks)
    success_rate = round(len(completed_tasks) / total_terminal, 2) if total_terminal > 0 else None

    # Previous window for trend comparison
    prev_completed = [
        t for t in all_tasks
        if t.status == TaskStatus.COMPLETED
        and prev_window_start <= _derive_completed_at(t) < window_start
    ]
    prev_failed = [
        t for t in all_tasks
        if t.status == TaskStatus.FAILED
        and prev_window_start <= _derive_completed_at(t) < window_start
    ]
    prev_total = len(prev_completed) + len(prev_failed)
    prev_success_rate = round(len(prev_completed) / prev_total, 2) if prev_total > 0 else None

    trend: Optional[str] = None
    if success_rate is not None and prev_success_rate is not None:
        delta = success_rate - prev_success_rate
        if delta > 0.05:
            trend = "improving"
        elif delta < -0.05:
            trend = "declining"
        else:
            trend = "stable"

    # Failure distribution (top 3 by count)
    failure_counts: Dict[str, int] = {}
    for t in failed_tasks:
        cls = _classify_failure(t)
        failure_counts[cls] = failure_counts.get(cls, 0) + 1
    failure_distribution = dict(
        sorted(failure_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    )

    # Direction stats: merge_rate and cycle count per active direction
    direction_stats: List[Dict[str, Any]] = []
    project = await session.get(Project, project_id)
    if project:
        active_directions = [d for d in (project.directions or []) if d.get("status") == "active"]

        dir_tasks: Dict[str, list] = {}
        for t in all_tasks:
            did = (t.context or {}).get("direction_id")
            if did:
                dir_tasks.setdefault(str(did), []).append(t)

        for d in active_directions:
            did = str(d.get("id") or d.get("direction_id") or "")
            if not did:
                continue

            tasks_for_dir = dir_tasks.get(did, [])
            cycles = sum(
                1 for t in tasks_for_dir
                if t.status == TaskStatus.COMPLETED
                and (t.first_payload).get("type", "create") == "research"
                and _derive_completed_at(t) >= window_start
            )

            if cycles == 0:
                continue

            prs_merged = 0
            for entry in d.get("research_history") or []:
                try:
                    entry_ts = datetime.fromisoformat(entry.get("timestamp", ""))
                    if entry_ts >= window_start:
                        prs_merged += entry.get("prs_merged") or 0
                except (ValueError, TypeError):
                    pass

            direction_stats.append({
                "direction_id": did,
                "merge_rate": round(prs_merged / cycles, 2),
                "cycles": cycles,
                "possibly_saturated": bool(d.get("possibly_saturated", False)),
            })

        direction_stats.sort(key=lambda x: x["cycles"], reverse=True)

    # First-attempt success rate: tasks that completed on the first try
    terminal_tasks_24h = completed_tasks + failed_tasks
    by_type: Dict[str, list] = {}
    for t in terminal_tasks_24h:
        task_type = (t.first_payload or {}).get("type", "create")
        by_type.setdefault(task_type, []).append(t)

    def _first_attempt_rate(tasks):
        if not tasks:
            return None
        first_success = sum(
            1 for t in tasks
            if (t.context or {}).get("_first_attempt_succeeded") is True
        )
        # Only count tasks that have the metric recorded
        measured = [t for t in tasks if "_first_attempt_succeeded" in (t.context or {})]
        if not measured:
            return None
        first_success = sum(1 for t in measured if t.context["_first_attempt_succeeded"])
        return round(first_success / len(measured), 2)

    tasks_with_playbook = [
        t for t in terminal_tasks_24h
        if (t.context or {}).get("_had_playbook") is True
        and "_first_attempt_succeeded" in (t.context or {})
    ]
    tasks_without_playbook = [
        t for t in terminal_tasks_24h
        if (t.context or {}).get("_had_playbook") is False
        and "_first_attempt_succeeded" in (t.context or {})
    ]

    first_attempt_by_type = {}
    for task_type, tasks in by_type.items():
        rate = _first_attempt_rate(tasks)
        if rate is not None:
            first_attempt_by_type[task_type] = rate

    first_attempt_success_rate = {
        "overall": _first_attempt_rate(terminal_tasks_24h),
        "with_playbook": _first_attempt_rate(tasks_with_playbook) if tasks_with_playbook else None,
        "without_playbook": _first_attempt_rate(tasks_without_playbook) if tasks_without_playbook else None,
        "sample_size": len([t for t in terminal_tasks_24h if "_first_attempt_succeeded" in (t.context or {})]),
        "by_type": first_attempt_by_type,
    }

    return {
        "success_rate": success_rate,
        "trend": trend,
        "failure_distribution": failure_distribution,
        "direction_stats": direction_stats,
        "first_attempt_success_rate": first_attempt_success_rate,
    }

@router.get("/digest")
async def get_digest(
    hours: int = Query(24, ge=1, le=168),
    project_id: Optional[uuid.UUID] = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin)
):
    """Return a catch-up digest of activity within the requested time window."""
    window_start = _utcnow() - timedelta(hours=hours)
    prev_window_start = window_start - timedelta(hours=hours)
    # Extend lookback to capture long-running tasks and 7 periods for trend history
    raw_lookback = _utcnow() - timedelta(hours=min(hours * 7, 24 * 14))
    lookback_start = min(raw_lookback, prev_window_start)

    def _derive_completed_at(t: Task):
        if t.status_log:
            try:
                return datetime.fromisoformat(t.status_log[-1]["t"])
            except (KeyError, ValueError):
                pass
        return t.created_at

    # --- Query tasks from extended lookback window ---
    task_filters = [Task.created_at >= lookback_start]
    if project_id:
        task_filters.append(Task.project_id == project_id)

    tasks_result = await session.execute(select(Task).where(*task_filters))
    all_tasks = tasks_result.scalars().all()

    # Post-filter completed/failed by completion timestamp, not creation time
    completed_tasks = [
        t for t in all_tasks
        if t.status == TaskStatus.COMPLETED and _derive_completed_at(t) >= window_start
    ]
    failed_tasks = [
        t for t in all_tasks
        if t.status == TaskStatus.FAILED and _derive_completed_at(t) >= window_start
    ]
    # Approval tasks use created_at (they haven't completed yet)
    approval_tasks = [
        t for t in all_tasks
        if t.requires_approval
        and t.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED)
        and t.created_at >= window_start
    ]

    # --- Query active tasks (not filtered by window — they may have started before it) ---
    _active_statuses = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    ]
    active_filters = [Task.status.in_(_active_statuses)]
    if project_id:
        active_filters.append(Task.project_id == project_id)
    active_result = await session.execute(
        select(Task).where(*active_filters).order_by(Task.started_at.asc().nullsfirst())
    )
    active_tasks_all = active_result.scalars().all()

    # research_cycles: completed tasks with task_type == 'research'
    research_cycles = sum(
        1 for t in completed_tasks
        if (t.first_payload).get("type", "create") == "research"
    )

    # --- Direction progress (only when project_id provided) ---
    direction_progress_list: List[Dict[str, Any]] = []
    dir_id_to_text: Dict[str, str] = {}

    if project_id:
        project = await session.get(Project, project_id)
        if project:
            active_directions = [d for d in (project.directions or []) if d.get("status") == "active"]

            # Build direction_id -> text lookup
            for d in active_directions:
                did = d.get("id") or d.get("direction_id")
                if did:
                    dir_id_to_text[str(did)] = d.get("text") or d.get("description") or ""

            # Build direction_id -> tasks mapping
            dir_tasks: Dict[str, List] = {}
            for t in all_tasks:
                did = (t.context or {}).get("direction_id")
                if did:
                    dir_tasks.setdefault(str(did), []).append(t)

            for d in active_directions:
                did = str(d.get("id") or d.get("direction_id") or "")
                if not did:
                    continue

                tasks_for_dir = dir_tasks.get(did, [])

                cycles_in_window = sum(
                    1 for t in tasks_for_dir
                    if t.status == TaskStatus.COMPLETED
                    and (t.first_payload).get("type", "create") == "research"
                    and _derive_completed_at(t) >= window_start
                )
                tasks_created_in_window = sum(
                    1 for t in tasks_for_dir
                    if t.created_at >= window_start
                )

                # prs_merged: sum from research_history entries within window
                prs_merged = 0
                for entry in d.get("research_history") or []:
                    try:
                        entry_ts = datetime.fromisoformat(entry.get("timestamp", ""))
                        if entry_ts >= window_start:
                            prs_merged += entry.get("prs_merged") or 0
                    except (ValueError, TypeError):
                        pass

                if cycles_in_window == 0 and tasks_created_in_window == 0:
                    continue

                last_entry = (d.get("research_history") or [])[-1] if d.get("research_history") else None
                latest_insights = last_entry.get("cycle_insights", []) if last_entry else []
                recent_finding_titles = last_entry.get("finding_titles", []) if last_entry else []
                latest_research_at = last_entry.get("timestamp") if last_entry else None

                direction_text = (d.get("text") or d.get("description") or "")[:80]
                direction_progress_list.append({
                    "direction_id": did,
                    "direction_text": direction_text,
                    "priority": d.get("priority") or "medium",
                    "cycles_in_window": cycles_in_window,
                    "tasks_created": tasks_created_in_window,
                    "prs_merged": prs_merged,
                    "possibly_saturated": bool(d.get("possibly_saturated", False)),
                    "latest_insights": latest_insights,
                    "recent_finding_titles": recent_finding_titles,
                    "latest_research_at": latest_research_at,
                })

            direction_progress_list.sort(key=lambda x: x["cycles_in_window"], reverse=True)

    # --- Batch artifact query ---
    all_task_ids = [t.id for t in all_tasks]
    pr_count = 0
    pr_artifact_map: Dict[str, str] = {}   # task_id -> pr_url
    error_log_map: Dict[str, str] = {}     # task_id -> truncated error snippet

    if all_task_ids:
        artifacts_result = await session.execute(
            select(Artifact)
            .where(
                Artifact.task_id.in_(all_task_ids),
                Artifact.artifact_type.in_([
                    ArtifactType.PULL_REQUEST,
                    ArtifactType.ERROR_LOG,
                ])
            )
            .order_by(Artifact.created_at.asc())
        )
        for a in artifacts_result.scalars().all():
            tid = str(a.task_id)
            if a.artifact_type == ArtifactType.PULL_REQUEST:
                if a.created_at >= window_start:
                    pr_count += 1
                pr_artifact_map[tid] = a.pr_url or ""
            elif a.artifact_type == ArtifactType.ERROR_LOG and a.logs and tid not in error_log_map:
                error_log_map[tid] = a.logs[:500]

    # --- needs_attention: failed + pending approval ---
    _now = _utcnow()
    needs_attention = []
    for t in failed_tasks:
        tid = str(t.id)
        waiting_since = _derive_completed_at(t)
        waiting_hours = round(((_now - waiting_since).total_seconds() / 3600)) if waiting_since else None
        needs_attention.append({
            "task_id": tid,
            "title": t.title or "Failed task",
            "reason": "failed",
            "error_snippet": error_log_map.get(tid),
            "pr_url": pr_artifact_map.get(tid),
            "waiting_since": waiting_since.isoformat() if waiting_since else None,
            "waiting_hours": waiting_hours,
        })
    for t in approval_tasks:
        tid = str(t.id)
        waiting_since = _derive_completed_at(t)
        waiting_hours = round(((_now - waiting_since).total_seconds() / 3600)) if waiting_since else None
        needs_attention.append({
            "task_id": tid,
            "title": t.title or "Pending approval",
            "reason": "approval_required",
            "error_snippet": None,
            "pr_url": pr_artifact_map.get(tid),
            "waiting_since": waiting_since.isoformat() if waiting_since else None,
            "waiting_hours": waiting_hours,
            "approval_reason": t.latest_status_message,
            "touched_critical": (t.context or {}).get("touched_critical", []),
        })
    needs_attention.sort(key=lambda x: (0 if x["reason"] == "approval_required" else 1, -(x["waiting_hours"] or 0)))

    # --- recent_failures (up to 10, desc) ---
    sorted_failures = sorted(failed_tasks, key=_derive_completed_at, reverse=True)
    recent_failures = []
    for t in sorted_failures[:10]:
        tid = str(t.id)
        ts = _derive_completed_at(t)
        waiting_since = _derive_completed_at(t)
        waiting_hours = round(((_now - waiting_since).total_seconds() / 3600)) if waiting_since else None
        recent_failures.append({
            "task_id": tid,
            "title": t.title or "Failed task",
            "failed_at": ts.isoformat() if ts else None,
            "error_snippet": error_log_map.get(tid),
            "waiting_hours": waiting_hours,
            "attempt_count": t.attempt_count,
        })

    # --- failure_groups (classify failures by type) ---
    from app.main import classify_failure as _classify_failure
    _failure_class_labels = {
        "CODE_ERROR": "Code Error",
        "PROVISION_FAILURE": "Provision Failure",
        "AGENT_TIMEOUT": "Agent Timeout",
        "EXTERNAL_API": "External API",
        "RATE_LIMIT": "Rate Limit",
    }
    _failure_class_groups: Dict[str, List[Dict[str, Any]]] = {}
    for t in failed_tasks:
        tid = str(t.id)
        cls = _classify_failure(t, error_log_map.get(tid))
        waiting_since = _derive_completed_at(t)
        waiting_hours = round(((_now - waiting_since).total_seconds() / 3600)) if waiting_since else None
        _failure_class_groups.setdefault(cls, []).append({
            "task_id": tid,
            "title": t.title or "Failed task",
            "error_snippet": error_log_map.get(tid),
            "attempt_count": t.attempt_count,
            "waiting_hours": waiting_hours,
        })
    failure_groups = sorted(
        [
            {
                "failure_class": cls,
                "label": _failure_class_labels.get(cls, cls),
                "count": len(tasks),
                "tasks": tasks,
            }
            for cls, tasks in _failure_class_groups.items()
        ],
        key=lambda x: x["count"],
        reverse=True,
    )

    # --- recent_completions (up to 10, desc) ---
    sorted_completions = sorted(completed_tasks, key=_derive_completed_at, reverse=True)
    recent_completions = []
    for t in sorted_completions[:10]:
        tid = str(t.id)
        ts = _derive_completed_at(t)
        task_type = (t.first_payload).get("type", "create")
        t_dir_id = (t.context or {}).get("direction_id")
        t_dir_text = dir_id_to_text.get(str(t_dir_id)) if t_dir_id else None
        recent_completions.append({
            "task_id": tid,
            "title": t.title or "Completed task",
            "completed_at": ts.isoformat() if ts else None,
            "pr_url": pr_artifact_map.get(tid),
            "task_type": task_type,
            "direction_id": t_dir_id,
            "direction_text": t_dir_text,
        })

    # --- active_tasks list (up to 5, oldest started first) ---
    active_tasks_list = [
        {
            "task_id": str(t.id),
            "title": t.title or "Active task",
            "status": t.status.value if hasattr(t.status, "value") else t.status,
            "task_type": (t.first_payload).get("type", "create"),
            "started_at": t.started_at.isoformat() if t.started_at else None,
        }
        for t in active_tasks_all[:5]
    ]

    running_statuses = {TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING}
    queued_statuses = {TaskStatus.QUEUED, TaskStatus.EVALUATING}
    running_count = sum(1 for t in active_tasks_all if t.status in running_statuses)
    queued_count = sum(1 for t in active_tasks_all if t.status in queued_statuses)

    total_terminal = len(completed_tasks) + len(failed_tasks)
    success_rate = round(len(completed_tasks) / total_terminal, 2) if total_terminal > 0 else None

    # --- narrative_summary ---
    completed_count = len(completed_tasks)
    failed_count = len(failed_tasks)
    pending_approval_count = len(approval_tasks)

    # --- Previous window metrics for trend comparison ---
    prev_completed_tasks = [
        t for t in all_tasks
        if t.status == TaskStatus.COMPLETED
        and prev_window_start <= _derive_completed_at(t) < window_start
    ]
    prev_failed_tasks = [
        t for t in all_tasks
        if t.status == TaskStatus.FAILED
        and prev_window_start <= _derive_completed_at(t) < window_start
    ]
    prev_total_terminal = len(prev_completed_tasks) + len(prev_failed_tasks)
    prev_success_rate = round(len(prev_completed_tasks) / prev_total_terminal, 2) if prev_total_terminal > 0 else None
    prev_completed_count = len(prev_completed_tasks)
    prev_failed_count = len(prev_failed_tasks)

    completed_delta = completed_count - prev_completed_count
    failed_delta = failed_count - prev_failed_count
    success_rate_delta: Optional[float] = (
        round(success_rate - prev_success_rate, 2)
        if success_rate is not None and prev_success_rate is not None
        else None
    )
    if success_rate_delta is not None:
        if success_rate_delta > 0.05:
            trend: Optional[str] = "improving"
        elif success_rate_delta < -0.05:
            trend = "declining"
        else:
            trend = "stable"
    else:
        trend = None

    # Compute trend history: 7 periods (6 previous + current), oldest first
    _now = _utcnow()
    trend_history = []
    for _i in range(7):
        _p_start = _now - timedelta(hours=hours * (7 - _i))
        _p_end = _now - timedelta(hours=hours * (6 - _i))
        _p_completed = sum(
            1 for t in all_tasks
            if t.status == TaskStatus.COMPLETED
            and _p_start <= _derive_completed_at(t) < _p_end
        )
        _p_failed = sum(
            1 for t in all_tasks
            if t.status == TaskStatus.FAILED
            and _p_start <= _derive_completed_at(t) < _p_end
        )
        _p_total = _p_completed + _p_failed
        trend_history.append({
            "period_end": _p_end.isoformat(),
            "success_rate": round(_p_completed / _p_total, 2) if _p_total > 0 else None,
            "completed": _p_completed,
            "failed": _p_failed,
        })

    # Action-first narrative: executives want "what do I need to do?" before "how did things go?"
    narrative_parts = []

    # All clear shortcut: nothing needs attention and there are completions
    if pending_approval_count == 0 and failed_count == 0 and completed_count > 0:
        all_clear = f"All clear — {completed_count} task{'s' if completed_count != 1 else ''} completed"
        if pr_count > 0:
            all_clear += f" with {pr_count} PR{'s' if pr_count != 1 else ''} merged"
        all_clear += ", no action needed."
        narrative_summary = all_clear
    else:
        # FIRST: Approval items — most actionable, lead with them
        if pending_approval_count > 0:
            approval_items = [
                x for x in needs_attention if x["reason"] == "approval_required"
            ]
            max_wait = max((x.get("waiting_hours") or 0 for x in approval_items), default=0)
            approval_str = f"{pending_approval_count} task{'s' if pending_approval_count != 1 else ''} awaiting your approval"
            if max_wait > 0:
                approval_str += f" ({max_wait}h)"
            narrative_parts.append(approval_str + ".")

        # SECOND: Failures with root cause
        if failed_count > 0:
            dominant_group = failure_groups[0] if failure_groups else None
            dominant_msg = None
            if dominant_group and dominant_group["count"] >= 2 and dominant_group["count"] / failed_count >= 0.6:
                cls = dominant_group["failure_class"]
                cnt = dominant_group["count"]
                _root_cause_msgs = {
                    "RATE_LIMIT": f"Most failures ({cnt}/{failed_count}) are rate-limit errors — consider pausing until account recovery.",
                    "PROVISION_FAILURE": f"Most failures ({cnt}/{failed_count}) are provisioning issues — check GCP quota and zone health.",
                    "EXTERNAL_API": f"Most failures ({cnt}/{failed_count}) are external API errors — GitHub/Claude API may be experiencing issues.",
                    "AGENT_TIMEOUT": f"Most failures ({cnt}/{failed_count}) are agent timeouts — tasks may need complexity reduction.",
                    "CODE_ERROR": f"Most failures ({cnt}/{failed_count}) are code errors in agent-written code.",
                }
                dominant_msg = _root_cause_msgs.get(cls)
            if dominant_msg:
                narrative_parts.append(dominant_msg)
            else:
                failure_titles = [t.title or "Failed task" for t in sorted_failures[:2]]
                failed_str = f"{failed_count} task{'s' if failed_count != 1 else ''} failed"
                if failure_titles:
                    failed_str += f" ({', '.join(failure_titles)})"
                narrative_parts.append(failed_str + ".")

        # THIRD: Completion stats with trend prefix embedded
        if completed_count > 0:
            trend_prefix = ""
            if trend == "declining":
                trend_prefix = "Pipeline health declining — "
            elif trend == "improving":
                trend_prefix = "Good progress — "
            shipped = f"{trend_prefix}{completed_count} task{'s' if completed_count != 1 else ''} completed"
            if pr_count > 0:
                shipped += f" with {pr_count} PR{'s' if pr_count != 1 else ''} opened"
            completion_titles = [t.title or "Completed task" for t in sorted_completions[:2]]
            completion_titles = [t[:50] + "..." if len(t) > 50 else t for t in completion_titles]
            if completion_titles:
                shipped += f" including {', '.join(completion_titles)}"
            if completed_delta > 0:
                shipped += f" (up from {prev_completed_count} last period)"
            elif completed_delta < 0:
                shipped += f" (down from {prev_completed_count} last period)"
            narrative_parts.append(shipped + ".")

        # FOURTH: Direction progress
        if project_id and direction_progress_list:
            dir_summaries = []
            for dp in direction_progress_list:
                dt = (dp["direction_text"] or "")[:40]
                if dp["prs_merged"] > 0:
                    dir_summaries.append(f"{dt}: {dp['prs_merged']} PR{'s' if dp['prs_merged'] != 1 else ''} merged")
                elif dp.get("possibly_saturated"):
                    dir_summaries.append(f"{dt}: may be saturated")
                elif dp["tasks_created"] > 0:
                    dir_summaries.append(f"{dt}: {dp['tasks_created']} task{'s' if dp['tasks_created'] != 1 else ''} in progress")
            if dir_summaries:
                narrative_parts.append("Research: " + "; ".join(dir_summaries) + ".")

        # FIFTH: Pipeline status
        if running_count > 0 or queued_count > 0:
            pipeline_parts = []
            if running_count > 0:
                pipeline_parts.append(f"{running_count} running")
            if queued_count > 0:
                pipeline_parts.append(f"{queued_count} queued")
            narrative_parts.append(", ".join(pipeline_parts) + ".")

        if narrative_parts:
            narrative_summary = " ".join(narrative_parts)
        else:
            narrative_summary = f"All quiet — no activity in the last {hours}h."

    return {
        "window_hours": hours,
        "generated_at": _utcnow().isoformat(),
        "completed_count": len(completed_tasks),
        "failed_count": len(failed_tasks),
        "pending_approval_count": len(approval_tasks),
        "research_cycles": research_cycles,
        "prs_opened": pr_count,
        "active_count": len(active_tasks_all),
        "running_count": running_count,
        "queued_count": queued_count,
        "active_tasks": active_tasks_list,
        "success_rate": success_rate,
        "needs_attention": needs_attention,
        "recent_failures": recent_failures,
        "failure_groups": failure_groups,
        "recent_completions": recent_completions,
        "direction_progress": direction_progress_list,
        "narrative_summary": narrative_summary,
        "prev_completed_count": prev_completed_count,
        "prev_failed_count": prev_failed_count,
        "prev_success_rate": prev_success_rate,
        "completed_delta": completed_delta,
        "failed_delta": failed_delta,
        "success_rate_delta": success_rate_delta,
        "trend": trend,
        "trend_history": trend_history,
    }


