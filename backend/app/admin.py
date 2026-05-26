from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from typing import Dict, Any, List
import os
import uuid
import json
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, update, case
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm import load_only

from app.database import get_session
from app.shared_state import (
    derive_task_branch as _derive_task_branch,
    limiter,
)
from app.models import (
    Task,
    TaskStatus,
    TaskHistory,
    Project,
    ProjectMember,
    Artifact,
    ArtifactType,
    DeploymentState,
    DeploymentStatus,
    _utcnow,
)
from app.auth import get_current_user
from app.task_locking import task_lock

from app.broadcast import broadcast_to_project
from app.payload_schemas import (
    TaskType,
    validate_internal_payload,
    validate_task_payload,
)
from app.routers.admin_analytics import ON_DEMAND_RATE, SPOT_RATE
from app.compute import _capture_serial_for_vm, get_vm_zone, get_vm_status
from app import release_qualifier, release_version


def _estimated_cost_usd(task: Task) -> float | None:
    """Return the VM cost for a task, or None when timing data is incomplete."""
    if not task.vm_provisioned_at or not task.vm_stopped_at:
        return None
    rate = SPOT_RATE if task.vm_spot else ON_DEMAND_RATE
    return (task.vm_stopped_at - task.vm_provisioned_at).total_seconds() * rate


# Per-loop max attempt budgets. lint/test mirror agent/src/constants.ts
# (MAX_LINT_FIX_ATTEMPTS=5, MAX_TEST_FIX_ATTEMPTS=3); "check" has no
# agent-side constant — 5 is a heuristic default used only for the
# high_iter > max_iter/2 trouble heuristic in this file.
_LOOP_MAX_ATTEMPTS: Dict[str, int] = {"lint": 5, "test": 3, "check": 5}
_FIX_LOOP_SUCCESS_REASONS = frozenset({"passed", "early_unchanged"})


def _fix_loop_summary(
    history: Dict[str, Any] | None,
) -> List[Dict[str, Any]] | None:
    """Return compact per-loop trouble entries from fix_loop_history, or None when healthy."""
    if not history:
        return None
    flagged = []
    for loop_name, attempts in history.items():
        if not isinstance(attempts, list) or not attempts:
            continue
        iterations = len(attempts)
        last = attempts[-1]
        last_exit_reason = last.get("exit_reason") if isinstance(last, dict) else None
        non_success = last_exit_reason not in _FIX_LOOP_SUCCESS_REASONS
        max_iter = _LOOP_MAX_ATTEMPTS.get(loop_name, 5)
        high_iter = iterations > max_iter / 2
        if non_success or high_iter:
            flagged.append(
                {
                    "loop": loop_name,
                    "iterations": iterations,
                    "last_exit_reason": last_exit_reason,
                    "non_success": non_success,
                }
            )
    return flagged if flagged else None


logger = logging.getLogger(__name__)


async def _terminal_cleanup_bg(
    parent_task_id: str,
    parent_vm_id: str | None,
    parent_acct_id: str | None,
    children_cleanup: List[tuple],
    force_kill: bool,
):
    """Run the slow VM/account cleanup for an admin-driven terminal transition.

    Scheduled via BackgroundTasks so the request handler can return immediately
    after the status writes commit. Holding a DB session through the 5s graceful
    shutdown sleep + serial GCE deletes used to starve the orchestrator's
    connection pool when the user cancelled tasks in bulk.

    `children_cleanup` is a list of (task_id_str, vm_id_or_none, acct_id_or_none).
    """
    from app.main import _ensure_vm_deleted
    from app.max_plan_pool import release_max_plan_account

    if parent_vm_id:
        if not force_kill:
            # Wait for the agent's next heartbeat to return 4xx so it shuts down
            # gracefully before we kill the VM out from under it.
            await asyncio.sleep(5)
        try:
            await _ensure_vm_deleted(parent_vm_id, reason="admin_terminal")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[ADMIN] BG cleanup: VM delete failed for parent={parent_task_id} vm={parent_vm_id}: {exc}"
            )
    if parent_acct_id:
        try:
            await release_max_plan_account(parent_acct_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[ADMIN] BG cleanup: account release failed for parent={parent_task_id} acct={parent_acct_id}: {exc}"
            )

    for child_task_id, child_vm_id, child_acct_id in children_cleanup:
        if child_vm_id:
            try:
                await _ensure_vm_deleted(child_vm_id, reason="parent_cancelled")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"[ADMIN] BG cleanup: VM delete failed for child={child_task_id} vm={child_vm_id}: {exc}"
                )
        if child_acct_id:
            try:
                await release_max_plan_account(child_acct_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"[ADMIN] BG cleanup: account release failed for child={child_task_id} acct={child_acct_id}: {exc}"
                )


def _record_failure_result(task: Task, failure_class: str) -> None:
    """Record a task failure for deployment monitoring and direction health.

    Safe to call alongside persist_failure_category; best-effort (logs on error).
    """
    try:
        from app.routers.agent_status import record_task_result
        from app.shared_state import record_direction_result

        record_task_result(
            success=False,
            failure_class=failure_class,
            task_id=str(task.id),
            task_type=(task.first_payload or {}).get("type", ""),
        )
        direction_id = str((task.context or {}).get("direction_id") or "")
        if direction_id:
            record_direction_result(
                direction_id,
                success=False,
                failure_class=failure_class,
                project_id=str(task.project_id) if task.project_id else "",
            )
    except Exception:
        logger.exception(f"[ADMIN] Failed to record failure result for task {task.id}")


router = APIRouter(
    prefix="/api/admin", tags=["admin"], dependencies=[Depends(get_current_user)]
)

_ROLE_LEVEL = {"viewer": 0, "member": 1, "owner": 2}


def _user_project_ids(user_id: str):
    """Subquery returning project IDs the user is a member of (any role)."""
    return select(ProjectMember.project_id).where(ProjectMember.user_id == user_id)


async def _get_user_project(
    project_id: uuid.UUID, user_id: str, session: AsyncSession
) -> Project:
    """Fetch a project ensuring the user is a member. Raises 404 if not found."""
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    member = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
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
    member = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Project not found")
    if _ROLE_LEVEL.get(member.role, 0) < _ROLE_LEVEL[min_role]:
        raise HTTPException(
            status_code=403, detail=f"Requires {min_role} role on this project"
        )
    return project


async def _get_user_task(
    task_id: uuid.UUID, user_id: str, session: AsyncSession
) -> Task:
    """Fetch a task ensuring the user is a member of its project. Raises 404 if not found."""
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    member = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == task.project_id,
                ProjectMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
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
    member = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == task.project_id,
                ProjectMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Task not found")
    if _ROLE_LEVEL.get(member.role, 0) < _ROLE_LEVEL[min_role]:
        raise HTTPException(
            status_code=403, detail=f"Requires {min_role} role on this project"
        )
    return task


TERMINAL_STATUSES = [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]

# Hard cap on /admin/tasks page size. A fully-populated task row can carry
# up to ~100KB of research_findings plus unbounded status_log, so 500 rows
# x large-tail payload stays safely under Cloud Run's 32 MB response limit.
ADMIN_TASKS_MAX_LIMIT = 500


@router.get("/tasks")
async def get_all_tasks(
    # le is generous for backward compat with clients that persisted larger
    # page sizes; the effective cap is ADMIN_TASKS_MAX_LIMIT below, which
    # bounds response size so it can't exceed Cloud Run's 32 MB body limit
    # as per-task payloads (research_findings, status_log) grow.
    limit: int = Query(50, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    status: List[TaskStatus] = Query(default=[]),
    terminal_only: bool = Query(False),
    archived: bool | None = Query(None),
    search: str | None = Query(None, max_length=200),
    created_after: datetime | None = Query(None),
    direction_id: str | None = Query(None),
    task_type: str | None = Query(None),
    spawned_by: str | None = Query(None, max_length=100),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    limit = min(limit, ADMIN_TASKS_MAX_LIMIT)

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
            (Task.title.ilike(search_pattern, escape="\\"))
            | (Task.description.ilike(search_pattern, escape="\\"))
            | (Task.latest_status_message.ilike(search_pattern, escape="\\"))
        )
    if created_after:
        # Task.created_at is TIMESTAMPTZ (alembic 0061) but the
        # NaiveUTCDateTime ORM TypeDecorator returns naive UTC; this strip
        # is now redundant but kept as defense-in-depth in case a future
        # query param arrives tz-aware (e.g. `...Z` or `...+00:00`) before
        # passing through the TypeDecorator's read path.
        if created_after.tzinfo is not None:
            created_after = created_after.astimezone(timezone.utc).replace(tzinfo=None)
        filters.append(Task.created_at >= created_after)
    if direction_id:
        filters.append(Task.context["direction_id"].as_string() == direction_id)
    if task_type:
        filters.append(Task.payloads[0]["type"].as_string() == task_type)
    if spawned_by:
        filters.append(Task.context["spawned_by"].as_string() == spawned_by)

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
    research_map: Dict[str, str] = {}
    commit_map: Dict[str, Dict[str, Any]] = {}
    if task_ids:
        artifact_stmt = (
            select(Artifact)
            .options(
                load_only(
                    Artifact.id,
                    Artifact.task_id,
                    Artifact.artifact_type,
                    Artifact.pr_url,
                    Artifact.commit_hash,
                    Artifact.repo_url,
                    Artifact.iteration,
                    Artifact.created_at,
                )
            )
            .where(Artifact.task_id.in_(task_ids))
            .order_by(Artifact.created_at.asc())
            .limit(len(task_ids) * 20)
        )
        artifacts = (await session.execute(artifact_stmt)).scalars().all()
        for a in artifacts:
            tid = str(a.task_id)
            if tid not in artifacts_map:
                artifacts_map[tid] = []
            artifacts_map[tid].append(
                {
                    "artifact_type": a.artifact_type,
                    "pr_url": a.pr_url,
                    "commit_hash": a.commit_hash,
                    "repo_url": a.repo_url,
                    "iteration": a.iteration,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
            )
            if a.artifact_type == ArtifactType.PULL_REQUEST and a.pr_url:
                pr_map[tid] = a.pr_url
            if a.artifact_type == ArtifactType.GIT_COMMIT and a.commit_hash:
                commit_map[tid] = {"commit_hash": a.commit_hash, "repo_url": a.repo_url}

        # Fetch logs columns separately to avoid loading large blobs in the main query
        logs_stmt = (
            select(Artifact)
            .options(load_only(Artifact.task_id, Artifact.artifact_type, Artifact.logs))
            .where(
                Artifact.task_id.in_(task_ids),
                Artifact.artifact_type.in_(
                    [ArtifactType.ERROR_LOG, ArtifactType.RESEARCH_FINDINGS]
                ),
            )
        )
        logs_artifacts = (await session.execute(logs_stmt)).scalars().all()
        for a in logs_artifacts:
            tid = str(a.task_id)
            if a.artifact_type == ArtifactType.ERROR_LOG and a.logs:
                logs_map[tid] = a.logs[:2000]
            if a.artifact_type == ArtifactType.RESEARCH_FINDINGS and a.logs:
                try:
                    parsed = await asyncio.to_thread(json.loads, a.logs[:100000])
                    if isinstance(parsed, list):
                        research_map[tid] = parsed
                    elif isinstance(parsed, dict):
                        logs = parsed.get("logs", [])
                        if isinstance(logs, list):
                            research_map[tid] = logs
                except (json.JSONDecodeError, AttributeError):
                    pass

    # Fetch task history (last 10 entries per task). A per-task ROW_NUMBER()
    # window keeps each task's 10 most-recent rows, so a single high-activity
    # task can't exhaust a global budget and starve other tasks of history.
    history_map: Dict[str, List[Dict[str, Any]]] = {}
    if task_ids:
        row_number = (
            func.row_number()
            .over(
                partition_by=TaskHistory.task_id,
                order_by=TaskHistory.timestamp.desc(),
            )
            .label("rn")
        )
        ranked = (
            select(TaskHistory, row_number)
            .where(TaskHistory.task_id.in_(task_ids))
            .subquery()
        )
        history_stmt = (
            select(ranked).where(ranked.c.rn <= 10).order_by(ranked.c.timestamp.desc())
        )
        all_history = (await session.execute(history_stmt)).all()
        for h in all_history:
            tid = str(h.task_id)
            if tid not in history_map:
                history_map[tid] = []
            history_map[tid].append(
                {
                    "timestamp": h.timestamp.isoformat() if h.timestamp else None,
                    "action": h.action,
                    "details": h.details,
                }
            )

    # Build parent→children map from the fetched result set (no extra DB query)
    children_map: Dict[str, List[str]] = {}
    for t in tasks:
        if t.parent_task_id:
            parent_key = str(t.parent_task_id)
            if parent_key not in children_map:
                children_map[parent_key] = []
            children_map[parent_key].append(str(t.id))

    # Return tasks with artifact data attached
    from app.failure_classification import classify_failure

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
            "last_heartbeat": t.last_heartbeat.isoformat()
            if t.last_heartbeat
            else None,
            # Audit R3-W2 (H1): expose heartbeat-hot columns directly so the
            # admin-ui System Metrics panel reads from the column source-of-truth.
            "last_metrics_json": t.last_metrics_json,
            "last_tool_use_at": t.last_tool_use_at.isoformat()
            if t.last_tool_use_at
            else None,
            "attempt_count": t.attempt_count,
            "payloads": t.payloads,
            "context": {
                k: v
                for k, v in (t.context or {}).items()
                if not k.startswith("max_plan_")
            },
            "archived": t.archived,
            "artifacts": artifacts_map.get(str(t.id), []),
            "pr_url": pr_map.get(str(t.id)),
            "error_logs": logs_map.get(str(t.id)),
            "research_findings": research_map.get(str(t.id)),
            "commit_hash": commit_map.get(str(t.id), {}).get("commit_hash"),
            "repo_url": commit_map.get(str(t.id), {}).get("repo_url"),
            "history": history_map.get(str(t.id), []),
            "child_task_ids": children_map.get(str(t.id), []),
            "failure_class": classify_failure(t)
            if t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
            and t.latest_status_message
            else None,
            "estimated_cost_usd": _estimated_cost_usd(t),
            "fix_loop_summary": _fix_loop_summary(t.fix_loop_history),
        }
        result_list.append(task_dict)

    # Global counts (not affected by pagination/filters) — single query via CASE/WHEN
    _counts_row = (
        await session.execute(
            select(
                func.count(
                    case(
                        (
                            Task.status.in_(
                                [TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING]
                            ),
                            1,
                        )
                    )
                ),
                func.count(case((Task.status == TaskStatus.EVALUATING, 1))),
                func.count(case((Task.status == TaskStatus.QUEUED, 1))),
            ).select_from(Task)
        )
    ).one()
    active_count, evaluating_count, queued_count = _counts_row

    return {
        "tasks": result_list,
        "total": total,
        "active_count": active_count,
        "evaluating_count": evaluating_count,
        "queued_count": queued_count,
    }


class BulkArchiveRequest(BaseModel):
    task_ids: List[str] | None = None
    filter: str | None = None  # 'completed' or 'terminal'


@router.post("/tasks/bulk-archive")
async def bulk_archive_tasks(
    request: BulkArchiveRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    user_project_ids = _user_project_ids(current_user["user_id"])
    if request.task_ids is not None:

        def _safe_uuid(raw: str) -> uuid.UUID | None:
            try:
                return uuid.UUID(raw)
            except (ValueError, AttributeError):
                return None

        valid_uuids = [u for raw_id in request.task_ids if (u := _safe_uuid(raw_id))]
        if not valid_uuids:
            return {"archived_count": 0}
        # Use SKIP LOCKED to avoid contending with concurrent orchestrator transactions
        # holding row locks. Skipping locked tasks is safe — terminal tasks normally aren't
        # locked, and if one is, we don't want to block archiving the rest of the column.
        select_stmt = (
            select(Task.id)
            .where(
                Task.id.in_(valid_uuids),
                Task.status.in_(TERMINAL_STATUSES),
                Task.archived.is_(False),
                Task.project_id.in_(user_project_ids),
            )
            .with_for_update(skip_locked=True)
        )
    elif request.filter in ("completed", "terminal"):
        statuses = (
            [TaskStatus.COMPLETED]
            if request.filter == "completed"
            else TERMINAL_STATUSES
        )
        select_stmt = (
            select(Task.id)
            .where(
                Task.archived.is_(False),
                Task.status.in_(statuses),
                Task.project_id.in_(user_project_ids),
            )
            .limit(5000)
            .with_for_update(skip_locked=True)
        )
    else:
        raise HTTPException(
            status_code=400,
            detail="Must provide task_ids or a valid filter ('completed' or 'terminal')",
        )

    lockable_ids = (await session.execute(select_stmt)).scalars().all()
    if not lockable_ids:
        return {"archived_count": 0}
    # Wave F4 C4: bulk Core UPDATEs bypass mapper-level `version_id_col`. F1
    # dropped that enforcement but keeps `version` as a forensic counter —
    # bump it here so concurrent-write detection via version drift remains
    # available even on archive paths.
    await session.execute(
        update(Task)
        .where(Task.id.in_(lockable_ids))
        .values(
            archived=True,
            version=Task.version + 1,
        )
    )
    await session.commit()
    return {"archived_count": len(lockable_ids), "has_more": len(lockable_ids) == 5000}


class BulkActionRequest(BaseModel):
    action: str  # 'approve' or 'retry'
    task_ids: List[str]


async def _bulk_retry_one_task(
    task_id: uuid.UUID,
    request_session: AsyncSession,
) -> str | None:
    """Run the retry mutation for a single task in its own short-lived session.

    Phase C2 (H-C3): the bulk-action retry path used to share the request
    session across all tasks AND call ``_requeue_parent_task`` (which clones
    failed children and remaps depends_on) inside that shared transaction.
    Two problems compounded: the long-held lock window blocked concurrent
    heartbeats, and a per-task failure rolled back the whole batch.

    Splitting per-task into its own session means: (a) each row lock is
    bounded by one task's mutation window, (b) a single failure doesn't
    nuke siblings, and (c) ``_requeue_parent_task`` runs scoped to one
    parent rather than queueing on the bulk transaction.

    The scoped session is built from ``request_session.bind`` so it shares
    the same engine — in production that's the module-level engine; in
    tests it's the test engine (in-memory SQLite or test Postgres). Mirrors
    the agent_status.py post-commit pattern.

    Returns the old vm_instance_id when the caller should schedule a VM
    delete after commit, or ``None`` for parent-type tasks (which clone
    children rather than spinning a VM here).
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.routers.agent import _terminated_task_ids
    from app.task_lifecycle import _requeue_parent_task

    scoped_factory = async_sessionmaker(
        request_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with scoped_factory() as scoped_session:
        async with task_lock(
            scoped_session, task_id, skip_locked=False, timeout=10
        ) as task:
            if task is None:
                # Row missing or unreachable — the caller already validated
                # membership in the claim-IDs phase, so this is a genuine
                # "deleted between phases" race; report it.
                raise HTTPException(status_code=404, detail="Task not found")

            task.status = TaskStatus.QUEUED
            # If the task was FAILED/CANCELLED, the SQLAlchemy set event added
            # task.id to _terminated_task_ids, revoking any outstanding JWT.
            # Drop the cache entry so the next provisioned VM can claim.
            _terminated_task_ids.pop(str(task.id), None)
            task_type = (task.first_payload).get("type", "")
            is_parent_type = task_type in ("research", "plan")
            children = []
            if is_parent_type:
                children = (
                    (
                        await scoped_session.execute(
                            select(Task).where(Task.parent_task_id == task.id)
                        )
                    )
                    .scalars()
                    .all()
                )
            old_vm: str | None = None
            if children:
                await _requeue_parent_task(task, scoped_session)
            else:
                old_vm = task.vm_instance_id
                task.attempt_count = 0
                task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                task.last_heartbeat = None
                # Reset retry counters for a clean slate — matches single-task admin retry
                bulk_ctx = dict(task.context or {})
                bulk_ctx.pop("provision_failures", None)
                bulk_ctx.pop("retry_after", None)
                bulk_ctx.pop("max_plan_account_id", None)
                bulk_ctx.pop("llm_account_id", None)
                bulk_ctx.pop("llm_provider", None)
                bulk_ctx.pop("llm_auth_mode", None)
                bulk_ctx.pop("llm_model", None)
                bulk_ctx.pop("llm_execution_mode", None)
                bulk_ctx.pop("blacklisted_accounts", None)
                bulk_ctx.pop("provision_failed_accounts", None)
                bulk_ctx.pop("_slot_released", None)
                bulk_ctx.pop("_api_failure_retries", None)
                task.context = bulk_ctx
            history = TaskHistory(
                task_id=task.id,
                action="MODIFIED",
                details={"changes": {"status": "QUEUED"}, "modified_by": "BULK_ACTION"},
            )
            scoped_session.add(task)
            scoped_session.add(history)
        await scoped_session.commit()
        return old_vm


@router.post("/tasks/bulk-action")
@limiter.limit("60/minute")
async def bulk_action_tasks(
    request: Request,
    body: BulkActionRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    # Wave F4 H10 — Accepted operational risk:
    # The 2-phase split below (Phase 1 claims IDs, Phase 2 mutates per-task)
    # has no orphan sweep if Cloud Run dies mid-iteration. Tasks claimed in
    # Phase 1 but not yet mutated in Phase 2 stay in their pre-bulk state;
    # the operator can re-issue the bulk action manually. This is acceptable
    # for solo-dev because Cloud Run mid-iteration crashes are rare. See
    # docs/db-deferrals.md ("Bulk-action orphan sweep") for the trigger to
    # add an automated sweep (>1 mid-iteration crash/month observed).
    if body.action not in ("approve", "retry", "cancel"):
        raise HTTPException(
            status_code=400, detail="action must be 'approve', 'retry', or 'cancel'"
        )

    results = []

    # ── Phase 1: claim IDs ──────────────────────────────────────────────────
    # Validate UUIDs and resolve membership in a single short transaction on
    # the shared request session. The pre-fetch returns light Task rows we
    # use to surface "not found" before falling through to per-task work.
    def _parse_task_uuid(raw: str) -> uuid.UUID | None:
        try:
            return uuid.UUID(raw)
        except (ValueError, AttributeError):
            return None

    valid_map: dict[str, uuid.UUID] = {}  # raw_id -> parsed UUID
    for raw_id in body.task_ids:
        parsed = _parse_task_uuid(raw_id)
        if parsed is not None:
            valid_map[raw_id] = parsed
        else:
            results.append(
                {"task_id": raw_id, "status": "error", "error": "Invalid task ID"}
            )
    if valid_map:
        member_project_ids = select(ProjectMember.project_id).where(
            ProjectMember.user_id == current_user["user_id"],
            ProjectMember.role.in_(["member", "owner"]),
        )
        fetched = {
            task.id: task
            for task in (
                await session.execute(
                    select(Task).where(
                        Task.id.in_(valid_map.values()),
                        Task.project_id.in_(member_project_ids),
                    )
                )
            )
            .scalars()
            .all()
        }
    else:
        fetched = {}

    # ── Phase 2: per-task processing ────────────────────────────────────────
    # Phase C2 (H-C3): retry path runs in its own scoped session per task so
    # _requeue_parent_task is not called inside the bulk transaction's lock
    # window. approve/cancel still use the shared session — they don't call
    # _requeue_parent_task and their per-task commits already release locks
    # immediately.
    for raw_id in body.task_ids:
        if raw_id not in valid_map:
            continue  # already reported as invalid
        task = fetched.get(valid_map[raw_id])
        if not task:
            results.append(
                {"task_id": raw_id, "status": "error", "error": "Task not found"}
            )
            continue

        try:
            if body.action == "retry":
                old_vm = await _bulk_retry_one_task(valid_map[raw_id], session)

                # Delete old VM after commit, same as modify_task
                if old_vm:
                    from app.main import _ensure_vm_deleted

                    await _ensure_vm_deleted(old_vm, reason="admin_requeue")

            elif body.action == "approve":
                was_blocked = task.requires_approval
                task.requires_approval = False

                if was_blocked:
                    logger.info(
                        f"[ADMIN] Task {task.id} bulk-approved (was blocked: {task.latest_status_message or 'unknown reason'})"
                    )

                if was_blocked and task.status in (
                    TaskStatus.COMPLETED,
                    TaskStatus.EVALUATING,
                ):
                    ctx = task.context or {}
                    rollup_pr_number = ctx.get("rollup_pr_number")

                    # Round 3 M10 — operator-response latency contract:
                    # The rollup-PR approval branch performs external GitHub
                    # API calls (merge_pull_request, create_pr_comment, and
                    # later _cleanup_branches) BEFORE per-task commit. The
                    # bulk endpoint's HTTP response therefore includes the
                    # full GitHub round-trip latency for each approved task
                    # (typically 2-5s, up to 30s+ during rate-limit). For N
                    # tasks the response time scales O(N) — operators should
                    # not assume sub-second latency on bulk approve. This is
                    # intentional: holding the merge external until commit
                    # would risk an inconsistency where the DB shows
                    # COMPLETED but the GitHub PR is still open.
                    if rollup_pr_number:
                        # Rollup PR approval: merge the rollup PR directly
                        try:
                            from app.routers.agent import get_github_token_for_task
                            from app.shared_state import (
                                runtime_config as _runtime_config,
                                get_project_promotion_target as _get_project_promotion_target,
                            )
                            from app.rollup_management import _cleanup_branches
                            from app.github import merge_pull_request, create_pr_comment

                            gh_token, owner, repo = await get_github_token_for_task(
                                task.id, session
                            )
                            merge_result = await merge_pull_request(
                                gh_token, owner, repo, rollup_pr_number
                            )
                            if merge_result.get("merged"):
                                task.status = TaskStatus.COMPLETED
                                task.context = {**ctx, "pr_merged": True}
                                task.latest_status_message = f"Human-approved merge of rollup PR #{rollup_pr_number}"
                                await create_pr_comment(
                                    gh_token,
                                    owner,
                                    repo,
                                    rollup_pr_number,
                                    "**Blueprint Review Pipeline**: Human-approved rollup merge.",
                                )
                                logger.info(
                                    f"[ADMIN] Bulk-approved rollup merge of PR #{rollup_pr_number}"
                                )
                                session.add(task)
                                await session.commit()
                                await _cleanup_branches(task, session)
                                base_branch = (task.first_payload).get(
                                    "base_branch", "main"
                                )
                                _project = await session.get(Project, task.project_id)
                                _promotion_target = _get_project_promotion_target(
                                    _project
                                )
                                if (
                                    base_branch == _promotion_target
                                    and _runtime_config.get("smoketest_enabled")
                                ):
                                    try:
                                        type_model = (
                                            _runtime_config.get(
                                                "task_type_models", {}
                                            ).get("smoketest")
                                            or None
                                        )
                                        type_effort = (
                                            _runtime_config.get(
                                                "task_type_efforts", {}
                                            ).get("smoketest")
                                            or None
                                        )
                                        health_check_urls = (
                                            _runtime_config.get("health_check_urls")
                                            or []
                                        )
                                        smoketest_task = Task(
                                            project_id=task.project_id,
                                            title=f"Smoketest after rollup merge to {base_branch}",
                                            description=f"Post-merge health checks for {owner}/{repo}@{base_branch}",
                                            payloads=[
                                                validate_internal_payload(
                                                    {
                                                        "type": "smoketest",
                                                        "base_branch": base_branch,
                                                        **(
                                                            {"model": type_model}
                                                            if type_model
                                                            else {}
                                                        ),
                                                        **(
                                                            {"effort": type_effort}
                                                            if type_effort
                                                            else {}
                                                        ),
                                                        **(
                                                            {
                                                                "health_check_urls": health_check_urls
                                                            }
                                                            if health_check_urls
                                                            else {}
                                                        ),
                                                    },
                                                    "admin:bulk_rollup_smoketest",
                                                )
                                            ],
                                            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                                            status=TaskStatus.QUEUED,
                                            created_by="ORCHESTRATOR",
                                        )
                                        session.add(smoketest_task)
                                        await session.commit()
                                        await session.refresh(smoketest_task)
                                        history_st = TaskHistory(
                                            task_id=smoketest_task.id,
                                            action="SPAWNED",
                                            details={
                                                "spawned_by": "ADMIN_BULK_ROLLUP_MERGE",
                                                "base_branch": base_branch,
                                            },
                                        )
                                        session.add(history_st)
                                        await session.commit()
                                        await broadcast_to_project(
                                            str(task.project_id),
                                            "task_updated",
                                            {
                                                "task_id": str(smoketest_task.id),
                                                "status": TaskStatus.QUEUED,
                                            },
                                        )
                                        logger.info(
                                            f"[SMOKETEST] Spawned smoketest task {smoketest_task.id} after bulk rollup merge to {base_branch}"
                                        )
                                    except Exception as e:  # noqa: BLE001
                                        logger.warning(
                                            f"[SMOKETEST] Failed to spawn smoketest after bulk rollup merge: {e}",
                                            exc_info=True,
                                        )
                            else:
                                task.latest_status_message = f"Approved but rollup merge failed: {merge_result.get('message', 'unknown')}"
                                logger.error(
                                    f"[ADMIN] Bulk-approved rollup merge of PR #{rollup_pr_number} failed: {merge_result.get('message')}"
                                )
                        except Exception as e:
                            task.latest_status_message = (
                                f"Approved but rollup merge failed: {str(e)[:200]}"
                            )
                            logger.error(
                                f"[ADMIN] Bulk-approved rollup merge failed: {e}",
                                exc_info=True,
                            )
                    else:
                        artifact = (
                            (
                                await session.execute(
                                    select(Artifact).where(
                                        Artifact.task_id == task.id,
                                        Artifact.artifact_type
                                        == ArtifactType.PULL_REQUEST,
                                    )
                                )
                            )
                            .scalars()
                            .first()
                        )
                        if artifact and artifact.pr_url:
                            pr_number = int(artifact.pr_url.rstrip("/").split("/")[-1])
                            task_payload = task.first_payload
                            _project = await session.get(Project, task.project_id)
                            branch = _derive_task_branch(
                                task.id,
                                task_payload.get("type"),
                                _project.task_branch_namespace,
                            )
                            base_branch = task_payload.get("base_branch", "main")

                            if ctx.get("touched_critical"):
                                try:
                                    from app.routers.agent import (
                                        get_github_token_for_task,
                                    )
                                    from app.github import (
                                        merge_pull_request,
                                        create_pr_comment,
                                        delete_branch,
                                    )

                                    (
                                        gh_token,
                                        owner,
                                        repo,
                                    ) = await get_github_token_for_task(
                                        task.id, session
                                    )
                                    merge_result = await merge_pull_request(
                                        gh_token, owner, repo, pr_number
                                    )
                                    if merge_result.get("merged"):
                                        task.status = TaskStatus.COMPLETED
                                        task.context = {**ctx, "pr_merged": True}
                                        task.latest_status_message = (
                                            f"Human-approved merge of PR #{pr_number}"
                                        )
                                        await create_pr_comment(
                                            gh_token,
                                            owner,
                                            repo,
                                            pr_number,
                                            "**Blueprint Review Pipeline**: Human-approved merge (security-critical files).",
                                        )
                                        try:
                                            await delete_branch(
                                                gh_token, owner, repo, branch
                                            )
                                        except Exception:
                                            logger.exception(
                                                f"[ADMIN] Failed to delete branch {branch} after merge"
                                            )
                                        logger.info(
                                            f"[ADMIN] Bulk-approved merge of PR #{pr_number} (critical files)"
                                        )
                                    else:
                                        task.latest_status_message = f"Approved but merge failed: {merge_result.get('message', 'unknown')}"
                                        logger.error(
                                            f"[ADMIN] Bulk-approved merge of PR #{pr_number} failed: {merge_result.get('message')}"
                                        )
                                except Exception as e:
                                    task.latest_status_message = (
                                        f"Approved but merge failed: {str(e)[:200]}"
                                    )
                                    logger.error(
                                        f"[ADMIN] Bulk-approved merge failed: {e}",
                                        exc_info=True,
                                    )
                            else:
                                from app.review_pipeline import _spawn_pipeline_task

                                original_prompt = task_payload.get(
                                    "prompt", task.description or ""
                                )
                                review_task, _was_created = await _spawn_pipeline_task(
                                    task,
                                    "review",
                                    session,
                                    pr_number,
                                    branch,
                                    base_branch,
                                    original_prompt=original_prompt,
                                    original_task_id=str(task.id),
                                )
                                if not review_task:
                                    task.requires_approval = True
                                    task.status = TaskStatus.FAILED
                                    if task.vm_provisioned_at:
                                        task.vm_stopped_at = _utcnow()
                                    task.latest_status_message = "Iteration cap reached — no follow-on task spawned"
                                    from app.failure_classification import (
                                        persist_failure_category,
                                    )

                                    persist_failure_category(task, "AGENT_TIMEOUT")
                                    _record_failure_result(task, "AGENT_TIMEOUT")
                                    session.add(task)
                                    await session.commit()
                                    results.append(
                                        {
                                            "task_id": raw_id,
                                            "status": "error",
                                            "error": "Iteration cap reached",
                                        }
                                    )
                                    continue

                history = TaskHistory(
                    task_id=task.id,
                    action="MODIFIED",
                    details={
                        "changes": {"requires_approval": False},
                        "modified_by": "BULK_ACTION",
                    },
                )
                session.add(task)
                session.add(history)
                await session.commit()

            elif body.action == "cancel":
                terminal_statuses = {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                }
                if task.status in terminal_statuses:
                    results.append(
                        {
                            "task_id": raw_id,
                            "status": "error",
                            "error": f"Task already in terminal state {task.status}",
                        }
                    )
                    continue

                task.status = TaskStatus.CANCELLED
                if task.vm_provisioned_at:
                    task.vm_stopped_at = _utcnow()
                task.requires_approval = False

                # Suppress low-priority tasks with finding_hash for 180 days
                if task.context and task.context.get("finding_hash"):
                    task_priority = task.priority if task.priority is not None else 50
                    if task_priority == 0:
                        suppress_until = (_utcnow() + timedelta(days=180)).isoformat()
                        task.context = {
                            **task.context,
                            "suppress_until": suppress_until,
                        }
                        flag_modified(task, "context")

                history = TaskHistory(
                    task_id=task.id,
                    action="MODIFIED",
                    details={
                        "changes": {"status": "CANCELLED"},
                        "modified_by": "BULK_ACTION",
                    },
                )
                session.add(task)
                session.add(history)
                # Commit terminal status first so agents detect cancellation
                await session.commit()

                # Delete VM immediately (no graceful sleep for bulk operations)
                if task.vm_instance_id:
                    from app.main import _ensure_vm_deleted

                    await _ensure_vm_deleted(task, reason="admin_bulk_cancel")
                    acct_id = (task.context or {}).get("max_plan_account_id")
                    if acct_id:
                        from app.max_plan_pool import release_max_plan_account

                        await release_max_plan_account(acct_id)

                # Cascade cancellation to active children
                from app.main import _ensure_vm_deleted as _cancel_vm
                from app.max_plan_pool import (
                    release_max_plan_account as _cancel_release,
                )

                active_children = (
                    (
                        await session.execute(
                            select(Task).where(
                                Task.parent_task_id == task.id,
                                Task.status.in_(
                                    [
                                        TaskStatus.QUEUED,
                                        TaskStatus.PROVISIONING,
                                        TaskStatus.IN_PROGRESS,
                                        TaskStatus.EVALUATING,
                                    ]
                                ),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for child in active_children:
                    child.status = TaskStatus.CANCELLED
                    if child.vm_provisioned_at:
                        child.vm_stopped_at = _utcnow()
                    child.requires_approval = False
                    session.add(child)
                await session.commit()
                for child in active_children:
                    if child.vm_instance_id:
                        await _cancel_vm(child, reason="parent_bulk_cancelled")
                    child_acct = (child.context or {}).get("max_plan_account_id")
                    if child_acct:
                        await _cancel_release(child_acct)
                if active_children:
                    logger.info(
                        f"[ADMIN] Bulk cancel cascaded to {len(active_children)} children of task {task.id}"
                    )

            results.append({"task_id": raw_id, "status": "ok"})
        except HTTPException as e:
            await session.rollback()
            results.append({"task_id": raw_id, "status": "error", "error": e.detail})
        except Exception as e:
            await session.rollback()
            logger.error(
                f"[ADMIN] Bulk action {body.action} failed for task {raw_id}: {e}",
                exc_info=True,
            )
            results.append(
                {"task_id": raw_id, "status": "error", "error": str(e)[:200]}
            )

    success_count = sum(1 for r in results if r["status"] == "ok")
    error_count = len(results) - success_count
    return {
        "results": results,
        "success_count": success_count,
        "error_count": error_count,
    }


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    try:
        tid = uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Task not found")

    task = await _get_user_task(tid, current_user["user_id"], session)

    # Fetch all artifacts for this task (all types, not just last per type)
    artifact_stmt = (
        select(Artifact)
        .where(Artifact.task_id == tid)
        .order_by(Artifact.created_at.asc())
    )
    artifacts = (await session.execute(artifact_stmt)).scalars().all()
    artifacts_list = []
    pr_url = None
    error_logs = None
    research_findings = None
    commit_hash = None
    repo_url = None
    for a in artifacts:
        artifacts_list.append(
            {
                "artifact_type": a.artifact_type,
                "pr_url": a.pr_url,
                "commit_hash": a.commit_hash,
                "repo_url": a.repo_url,
                "iteration": a.iteration,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
        )
        if a.artifact_type == ArtifactType.PULL_REQUEST and a.pr_url:
            pr_url = a.pr_url
        if a.artifact_type == ArtifactType.ERROR_LOG and a.logs:
            error_logs = a.logs
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
        "last_heartbeat": task.last_heartbeat.isoformat()
        if task.last_heartbeat
        else None,
        # Audit R3-W2 (H1): expose the heartbeat-hot columns directly so the
        # admin-ui System Metrics panel reads from the column source-of-truth.
        # Round 2 F retired the ctx.last_metrics JSONB mirror; this endpoint
        # was the missing piece that left the frontend silently stale.
        "last_metrics_json": task.last_metrics_json,
        "last_tool_use_at": task.last_tool_use_at.isoformat()
        if task.last_tool_use_at
        else None,
        "attempt_count": task.attempt_count,
        "payloads": task.payloads,
        "context": {
            k: v
            for k, v in (task.context or {}).items()
            if not k.startswith("max_plan_")
        },
        "archived": task.archived,
        "artifacts": artifacts_list,
        "pr_url": pr_url,
        "error_logs": error_logs,
        "commit_hash": commit_hash,
        "repo_url": repo_url,
        "research_findings": research_findings,
        "history": history,
        "child_task_ids": child_task_ids,
        "estimated_cost_usd": _estimated_cost_usd(task),
    }


@router.get("/tasks/{task_id}/serial-console")
async def get_task_serial_console(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Fetch the live GCE serial console for a task's VM.

    Returns the current serial console output for a task that has an active VM,
    or cached serial log from the last reap if the VM is no longer available.
    Returns 404 if the task has no vm_instance_id or doesn't exist.

    Response shape:
    {
        "vm_instance_id": string,
        "zone": string | null,
        "vm_status": string | null,
        "serial_log": string | null,
        "captured_at": ISO-8601 string,
        "source": "live" | "cached"
    }
    """
    try:
        tid = uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Task not found")

    task = await _get_user_task(tid, current_user["user_id"], session)

    # 404 if task has no VM
    if not task.vm_instance_id:
        raise HTTPException(status_code=404, detail="Task has no vm_instance_id")

    vm_id = task.vm_instance_id
    # _vm_zones is process-local; on multi-replica deploys this replica may
    # not have provisioned the VM. Fall back to the zone persisted in
    # task.context (mirrors the reaper.py provision_zone resolution pattern).
    zone = get_vm_zone(vm_id)
    if zone == "unknown":
        ctx_zone = (task.context or {}).get("provision_zone")
        if ctx_zone:
            zone = ctx_zone

    # Try to fetch live serial console
    live_serial = await asyncio.to_thread(_capture_serial_for_vm, vm_id, zone)

    # Get VM status
    vm_status = None
    if zone and zone != "unknown":
        vm_status = await asyncio.to_thread(get_vm_status, vm_id, zone)

    # Determine source and fall back to cached if needed
    serial_log = live_serial
    source = "live"
    if serial_log is None:
        # Fall back to cached serial log from reaper
        serial_log = (task.context or {}).get("last_serial_log")
        if serial_log is not None:
            source = "cached"

    return {
        "vm_instance_id": vm_id,
        "zone": zone if zone != "unknown" else None,
        "vm_status": vm_status,
        "serial_log": serial_log,
        "captured_at": _utcnow().isoformat(),
        "source": source,
    }


class CreateTaskPayload(BaseModel):
    github_repo: str = Field(..., pattern=r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")
    title: str = Field(..., min_length=1, max_length=200)
    prompt: str = Field(..., min_length=1, max_length=50000)
    base_branch: str = Field(
        default="main", max_length=255, pattern=r"^[a-zA-Z0-9_./-]*[a-zA-Z0-9_.-]$"
    )
    task_type: TaskType = Field(default=TaskType.create)
    pr_number: int | None = None
    branch: str | None = Field(
        default=None, max_length=255, pattern=r"^[a-zA-Z0-9_./-]*[a-zA-Z0-9_.-]$"
    )
    model: str | None = Field(default=None, min_length=1)
    effort: str | None = Field(default=None, pattern=r"^(low|medium|high|max)$")
    # Hotfix path (Phase 5 / automain-LTS): set hotfix=True together with
    # base_branch="main" to mark a plan task as an expedited fix off main.
    # The agent handler branches hotfix/* off main and the orchestrator
    # back-merges main → automain after the hotfix lands.
    hotfix: bool = False

    @field_validator("base_branch", "branch")
    @classmethod
    def validate_branch_name(cls, v: str | None) -> str | None:
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

    @model_validator(mode="after")
    def validate_pr_required_for_review_fix(self) -> "CreateTaskPayload":
        if self.task_type in (TaskType.review, TaskType.fix) and self.pr_number is None:
            raise ValueError(
                f"pr_number is required for task_type '{self.task_type.value}'"
            )
        return self


@router.post("/tasks")
@limiter.limit("60/minute")
async def create_task_manually(
    request: Request,
    payload: CreateTaskPayload,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """
    Create a Claude Code task. Auto-creates a project for the repo if one doesn't exist.

    Hotfix path: submit task_type="plan", base_branch="main", hotfix=true to
    launch an expedited fix off main (Phase 5 / automain-LTS).  The hotfix
    flag is stored in the task payload and picked up by the agent handler,
    which branches hotfix/* off main; the orchestrator back-merges
    main → automain after the hotfix lands.
    """
    # Guard: hotfix is only meaningful for `plan` tasks. The Phase 5 back-merge
    # trigger (task_lifecycle.py) only fires for a hotfix ROLLUP merge of a
    # `plan` task → main. A create/fix hotfix has no plan and thus no rollup,
    # so its PR would merge straight to main with NO back-merge — silently
    # diverging `automain` from `main` and breaking the next promotion.
    if payload.hotfix and payload.task_type != TaskType.plan:
        raise HTTPException(
            status_code=400,
            detail=(
                "hotfix=true is only supported for task_type='plan' "
                f"(got '{payload.task_type.value}'); a non-plan hotfix would "
                "merge to main without the automain back-merge."
            ),
        )

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
        session.add(
            ProjectMember(
                project_id=project.id, user_id=current_user["user_id"], role="owner"
            )
        )
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
    if payload.hotfix:
        task_payload["hotfix"] = True
    # Review/fix tasks need pr_number and branch
    if payload.task_type in ("review", "fix") and payload.pr_number:
        task_payload["pr_number"] = payload.pr_number
        task_payload["branch"] = payload.branch or ""
        if payload.task_type == "review":
            task_payload["original_prompt"] = payload.prompt
    # Research tasks auto-include project directions. For ad-hoc manual
    # spawns we ship lightweight direction summaries (id/text/priority/
    # status/system + truncated research_context) — full
    # research_context + research_history can be tens of KB per direction
    # and easily blow past validate_task_payload's 190 KB cap on projects
    # with many fat directions. The agent can fetch full direction state
    # via ``./fetch direction <id>`` when it needs it.
    if payload.task_type == "research":
        active_directions = [
            d for d in (project.directions or []) if d.get("status") == "active"
        ]
        trimmed_directions = []
        for d in active_directions:
            if not isinstance(d, dict):
                continue
            ctx = d.get("research_context") or ""
            entry = {
                "id": d.get("id"),
                "text": d.get("text"),
                "priority": d.get("priority"),
                "status": d.get("status"),
            }
            if d.get("system"):
                entry["system"] = d.get("system")
            if isinstance(ctx, str) and ctx:
                entry["research_context"] = ctx[:2000]
            trimmed_directions.append(entry)
        task_payload["directions"] = trimmed_directions

    try:
        task_payload = validate_task_payload(task_payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # Guard: cap active tasks per project to prevent queue explosion
    active_count = (
        await session.execute(
            select(func.count(Task.id)).where(
                Task.project_id == project.id,
                Task.status.in_(
                    [
                        TaskStatus.QUEUED,
                        TaskStatus.PROVISIONING,
                        TaskStatus.IN_PROGRESS,
                        TaskStatus.EVALUATING,
                    ]
                ),
            )
        )
    ).scalar() or 0
    if active_count >= 200:
        raise HTTPException(
            status_code=429,
            detail=f"Project has {active_count} active tasks (limit: 200)",
        )

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
        },
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
    current_user: dict = Depends(get_current_user),
):
    """Request changes on a task awaiting approval. Spawns a fix or re-review task."""
    task = await _get_user_task_with_role(
        task_id, current_user["user_id"], session, "member"
    )
    if not task.requires_approval:
        raise HTTPException(status_code=400, detail="Task is not awaiting approval")

    # Find the PR
    artifact = (
        (
            await session.execute(
                select(Artifact).where(
                    Artifact.task_id == task_id,
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                )
            )
        )
        .scalars()
        .first()
    )
    if not artifact or not artifact.pr_url:
        raise HTTPException(status_code=400, detail="No PR found for this task")

    pr_number = int(artifact.pr_url.rstrip("/").split("/")[-1])
    task_payload = task.first_payload
    _project = await session.get(Project, task.project_id)
    branch = _derive_task_branch(
        task.id, task_payload.get("type"), _project.task_branch_namespace
    )
    base_branch = task_payload.get("base_branch", "main")

    from app.review_pipeline import _spawn_pipeline_task

    if payload.action == "fix":
        spawned, _ = await _spawn_pipeline_task(
            task,
            "fix",
            session,
            pr_number,
            branch,
            base_branch,
            fix_prompt=f"Human reviewer requested changes:\n\n{payload.feedback}",
            original_task_id=str(task.id),
        )
    else:
        spawned, _ = await _spawn_pipeline_task(
            task,
            "review",
            session,
            pr_number,
            branch,
            base_branch,
            original_prompt=task_payload.get("prompt", task.description or ""),
            original_task_id=str(task.id),
        )

    if spawned:
        task.requires_approval = False
        task.latest_status_message = (
            f"Changes requested — spawned {payload.action} task"
        )
        session.add(task)
        await session.commit()
        return {"spawned_task_id": str(spawned.id), "action": payload.action}

    raise HTTPException(status_code=500, detail="Failed to spawn task (iteration cap?)")


_FRESH_REVIEWER_COOLDOWN_SECS = 3600  # 1 hour

# Maps a task's payload ``type`` to the branch prefix the agent uses when
# deriving the work branch. Mirrors the agent's branchPrefix wiring
# (agent/src/handlers/standard.ts: create/qa->task, refactor->refactor,
# docs->docs, deps->deps; handlers/specialized.ts->migrate). Fix tasks reuse
# an existing branch carried in the payload, so they never need this fallback.
# Branch derivation moved to app.shared_state.derive_task_branch so callers
# outside admin (rollup_management, task_lifecycle) can share the same map
# without circular imports.


@router.post("/tasks/{task_id}/request-fresh-reviewer")
@limiter.limit("10/minute")
async def request_fresh_reviewer(
    request: Request,
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Spawn an operator-requested fresh-reviewer pass against a completed task's PR diff."""
    task = await _get_user_task_with_role(
        task_id, current_user["user_id"], session, "member"
    )

    ctx = dict(task.context or {})
    last_req = ctx.get("_operator_fresh_reviewer_requested_at")
    if last_req:
        elapsed = (
            datetime.now(timezone.utc) - datetime.fromisoformat(last_req)
        ).total_seconds()
        if elapsed < _FRESH_REVIEWER_COOLDOWN_SECS:
            raise HTTPException(
                status_code=429,
                detail="Cooldown active — a fresh-reviewer pass was recently requested",
            )

    artifact = (
        (
            await session.execute(
                select(Artifact).where(
                    Artifact.task_id == task_id,
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                )
            )
        )
        .scalars()
        .first()
    )
    if not artifact or not artifact.pr_url:
        raise HTTPException(status_code=400, detail="No PR found for this task")

    pr_number = int(artifact.pr_url.rstrip("/").split("/")[-1])
    task_payload = task.first_payload
    review_loop = ctx.get("review_loop") or {}
    _project = await session.get(Project, task.project_id)
    branch = (
        review_loop.get("branch")
        or task_payload.get("branch")
        or _derive_task_branch(
            task_id, task_payload.get("type"), _project.task_branch_namespace
        )
    )
    base_branch = task_payload.get("base_branch", "main")
    related_files = task_payload.get("related_files") or ctx.get("related_files") or []

    reviewer_task = Task(
        project_id=task.project_id,
        title=f"Operator fresh review: {task.title or str(task_id)[:8]}",
        description=f"Operator-requested fresh-reviewer pass for task {task_id}",
        payloads=[
            {
                "type": "create",
                "prompt": f"Operator-requested fresh-reviewer pass for task {task_id}.",
                "pr_number": pr_number,
                "branch": branch,
                "base_branch": base_branch,
                "related_files": related_files,
            }
        ],
        context={
            "operator_requested_review_for": str(task_id),
            "operator_requested_by": str(current_user["user_id"]),
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="USER",
        launched_by_user_id=current_user["user_id"],
    )
    session.add(reviewer_task)

    ctx["_operator_fresh_reviewer_requested_at"] = datetime.now(
        timezone.utc
    ).isoformat()
    task.context = ctx
    flag_modified(task, "context")
    session.add(task)

    await session.commit()

    return {"spawned_task_id": str(reviewer_task.id)}


@router.post("/tasks/{task_id}/archive")
async def archive_task(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    task = await _get_user_task_with_role(
        task_id, current_user["user_id"], session, "member"
    )
    if task.status not in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot archive task with status '{task.status}'. Only terminal tasks (COMPLETED, FAILED, CANCELLED) can be archived.",
        )
    task.archived = True
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@router.post("/tasks/{task_id}/reset-iteration-cap")
async def reset_iteration_cap(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Reset the review-fix iteration cap for a PR.

    Archives FAILED review/fix tasks for the PR (drops them from the per-PR
    global count) and resets the per-chain iteration counter on ``task``.
    Use when the cap tripped on infra/transient failures rather than real
    review iterations, to let the pipeline retry without manual merge.

    Does NOT re-spawn tasks automatically — after reset, re-trigger the
    rollup (POST /tasks/{task_id}/rollup) or let the next orchestrator cycle
    pick up the parking task.
    """
    task = await _get_user_task_with_role(
        task_id, current_user["user_id"], session, "member"
    )

    ctx = task.context or {}
    review_loop = ctx.get("review_loop", {}) or {}
    pr_number = review_loop.get("pr_number") or ctx.get("rollup_pr_number")
    if not pr_number:
        raise HTTPException(
            status_code=400,
            detail="Task has no PR number in context.review_loop.pr_number or context.rollup_pr_number",
        )

    # Archive FAILED review/fix tasks on this PR so they drop from the global count.
    # _pr_number_match is dialect-conditional: on SQLite, .as_string() JSON_QUOTE-
    # double-quotes the int and never matches, so reuse the shared helper which
    # falls back to func.json_extract for SQLite.
    from app.ci_completion import _pr_number_match

    failed_result = await session.execute(
        select(Task)
        .where(
            Task.project_id == task.project_id,
            Task.status == TaskStatus.FAILED,
            Task.archived.is_(False),
            _pr_number_match(session, pr_number),
        )
        .options(load_only(Task.id, Task.archived, Task.payloads))
    )
    archived_count = 0
    for t in failed_result.scalars().all():
        fp = t.first_payload
        if fp.get("type") not in ("review", "fix"):
            continue
        t.archived = True
        session.add(t)
        archived_count += 1

    # Reset per-chain iteration counter on the parking task
    new_ctx = dict(ctx)
    new_review_loop = dict(review_loop)
    previous_iteration = new_review_loop.get("iteration", 0)
    new_review_loop["iteration"] = 0
    new_review_loop["iteration_reset_at"] = _utcnow().isoformat()
    new_review_loop["iteration_reset_by"] = current_user.get(
        "email", current_user["user_id"]
    )
    new_ctx["review_loop"] = new_review_loop
    task.context = new_ctx
    flag_modified(task, "context")

    # Clear requires_approval if it was set by the cap path
    was_requires_approval = task.requires_approval
    if task.requires_approval:
        task.requires_approval = False

    session.add(task)
    await session.commit()
    await session.refresh(task)
    logger.info(
        f"[ADMIN] Iteration cap reset on task {task.id} (PR #{pr_number}): "
        f"archived {archived_count} failed review/fix tasks, "
        f"per-chain iteration {previous_iteration}→0"
    )
    return {
        "task_id": str(task.id),
        "pr_number": pr_number,
        "archived_count": archived_count,
        "previous_per_chain_iteration": previous_iteration,
        "requires_approval_cleared": was_requires_approval,
    }


@router.post("/tasks/{task_id}/unarchive")
async def unarchive_task(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    task = await _get_user_task_with_role(
        task_id, current_user["user_id"], session, "member"
    )
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
    current_user: dict = Depends(get_current_user),
):
    task = await _get_user_task_with_role(
        task_id, current_user["user_id"], session, "member"
    )

    _TERMINAL_STATUSES = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}

    changes = {}
    if "status" in payload:
        try:
            new_status = TaskStatus(payload["status"])

            # State machine validation: admins can always requeue or cancel.
            # Only block nonsensical transitions like FAILED → IN_PROGRESS.
            admin_allowed = {TaskStatus.QUEUED, TaskStatus.CANCELLED}
            if new_status not in admin_allowed and task.status in _TERMINAL_STATUSES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot transition from terminal state {task.status} to {new_status} (only requeue or cancel is allowed)",
                )

            task.status = new_status
            changes["status"] = new_status.value

            # Clear approval flag on terminal statuses
            if new_status in (TaskStatus.CANCELLED, TaskStatus.FAILED):
                task.requires_approval = False

            # Capture cleanup info for terminal transitions; the actual VM delete
            # + account release happens in the background after the response is
            # sent, so the request handler doesn't hold its DB session through
            # 5s sleeps and serial GCE calls (which previously starved the
            # orchestrator's connection pool during bulk cancellations).
            parent_vm_id_for_bg: str | None = None
            parent_acct_id_for_bg: str | None = None
            if (
                new_status in (TaskStatus.CANCELLED, TaskStatus.FAILED)
                and task.vm_instance_id
            ):
                parent_vm_id_for_bg = task.vm_instance_id
                parent_acct_id_for_bg = (task.context or {}).get("max_plan_account_id")

            # For low-priority tasks cancelled by user, store suppress_until so the
            # orchestrator can skip re-proposals for 180 days without expanding the dedup window
            if (
                new_status == TaskStatus.CANCELLED
                and task.context
                and task.context.get("finding_hash")
            ):
                task_priority = task.priority if task.priority is not None else 50
                if (
                    task_priority == 0
                ):  # low priority tasks map to 0 in TASK_PRIORITY_MAP
                    suppress_until = (_utcnow() + timedelta(days=180)).isoformat()
                    task.context = {**task.context, "suppress_until": suppress_until}
                    flag_modified(task, "context")
                    logger.info(
                        f"[ADMIN] Set suppress_until={suppress_until} on cancelled low-priority task {task.id}"
                    )

            # Cascade cancellation to active children — only the cheap status
            # updates run synchronously; VM deletes + account releases are
            # deferred to the same background task.
            children_cleanup_for_bg: List[tuple] = []
            if new_status == TaskStatus.CANCELLED:
                active_children = (
                    (
                        await session.execute(
                            select(Task).where(
                                Task.parent_task_id == task.id,
                                Task.status.in_(
                                    [
                                        TaskStatus.QUEUED,
                                        TaskStatus.PROVISIONING,
                                        TaskStatus.IN_PROGRESS,
                                        TaskStatus.EVALUATING,
                                    ]
                                ),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for child in active_children:
                    child.status = TaskStatus.CANCELLED
                    if child.vm_provisioned_at:
                        child.vm_stopped_at = _utcnow()
                    child.requires_approval = False
                    children_cleanup_for_bg.append(
                        (
                            str(child.id),
                            child.vm_instance_id,
                            (child.context or {}).get("max_plan_account_id"),
                        )
                    )
                    session.add(child)
                if active_children:
                    logger.info(
                        f"[ADMIN] Cascaded CANCELLED to {len(active_children)} children of task {task.id}"
                    )

            if parent_vm_id_for_bg or parent_acct_id_for_bg or children_cleanup_for_bg:
                background_tasks.add_task(
                    _terminal_cleanup_bg,
                    parent_task_id=str(task.id),
                    parent_vm_id=parent_vm_id_for_bg,
                    parent_acct_id=parent_acct_id_for_bg,
                    children_cleanup=children_cleanup_for_bg,
                    force_kill=force_kill,
                )

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
                    children = (
                        (
                            await session.execute(
                                select(Task).where(Task.parent_task_id == task.id)
                            )
                        )
                        .scalars()
                        .all()
                    )
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
                    requeue_ctx.pop("llm_account_id", None)
                    requeue_ctx.pop("llm_provider", None)
                    requeue_ctx.pop("llm_auth_mode", None)
                    requeue_ctx.pop("llm_model", None)
                    requeue_ctx.pop("llm_execution_mode", None)
                    requeue_ctx.pop("blacklisted_accounts", None)
                    requeue_ctx.pop("provision_failed_accounts", None)
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
            logger.info(
                f"[ADMIN] Task {task.id} manually approved (was blocked: {task.latest_status_message or 'unknown reason'})"
            )

        # Approval flow: if unblocking a completed task with a PR, continue pipeline
        if (
            was_blocked
            and not task.requires_approval
            and task.status in (TaskStatus.COMPLETED, TaskStatus.EVALUATING)
        ):
            ctx = task.context or {}
            rollup_pr_number = ctx.get("rollup_pr_number")

            if rollup_pr_number:
                # Rollup PR approval: merge the rollup PR directly
                try:
                    from app.routers.agent import get_github_token_for_task
                    from app.shared_state import (
                        runtime_config as _runtime_config,
                        get_project_promotion_target as _get_project_promotion_target,
                    )
                    from app.rollup_management import _cleanup_branches
                    from app.github import merge_pull_request, create_pr_comment

                    gh_token, owner, repo = await get_github_token_for_task(
                        task.id, session
                    )
                    merge_result = await merge_pull_request(
                        gh_token, owner, repo, rollup_pr_number
                    )
                    if merge_result.get("merged"):
                        task.status = TaskStatus.COMPLETED
                        task.context = {**ctx, "pr_merged": True}
                        task.latest_status_message = (
                            f"Human-approved merge of rollup PR #{rollup_pr_number}"
                        )
                        await create_pr_comment(
                            gh_token,
                            owner,
                            repo,
                            rollup_pr_number,
                            "**Blueprint Review Pipeline**: Human-approved rollup merge.",
                        )
                        changes["merged_pr"] = rollup_pr_number
                        logger.info(
                            f"[ADMIN] Human-approved rollup merge of PR #{rollup_pr_number}"
                        )
                        session.add(task)
                        await session.commit()
                        # Post-merge cleanup: branches + smoketest (mirrors webhook merge path)
                        await _cleanup_branches(task, session)
                        base_branch = (task.first_payload).get("base_branch", "main")
                        _project = await session.get(Project, task.project_id)
                        _promotion_target = _get_project_promotion_target(_project)
                        if base_branch == _promotion_target and _runtime_config.get(
                            "smoketest_enabled"
                        ):
                            try:
                                type_model = (
                                    _runtime_config.get("task_type_models", {}).get(
                                        "smoketest"
                                    )
                                    or None
                                )
                                type_effort = (
                                    _runtime_config.get("task_type_efforts", {}).get(
                                        "smoketest"
                                    )
                                    or None
                                )
                                health_check_urls = (
                                    _runtime_config.get("health_check_urls") or []
                                )
                                smoketest_task = Task(
                                    project_id=task.project_id,
                                    title=f"Smoketest after rollup merge to {base_branch}",
                                    description=f"Post-merge health checks for {owner}/{repo}@{base_branch}",
                                    payloads=[
                                        validate_internal_payload(
                                            {
                                                "type": "smoketest",
                                                "base_branch": base_branch,
                                                **(
                                                    {"model": type_model}
                                                    if type_model
                                                    else {}
                                                ),
                                                **(
                                                    {"effort": type_effort}
                                                    if type_effort
                                                    else {}
                                                ),
                                                **(
                                                    {
                                                        "health_check_urls": health_check_urls
                                                    }
                                                    if health_check_urls
                                                    else {}
                                                ),
                                            },
                                            "admin:rollup_smoketest",
                                        )
                                    ],
                                    vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                                    status=TaskStatus.QUEUED,
                                    created_by="ORCHESTRATOR",
                                )
                                session.add(smoketest_task)
                                await session.commit()
                                await session.refresh(smoketest_task)
                                history = TaskHistory(
                                    task_id=smoketest_task.id,
                                    action="SPAWNED",
                                    details={
                                        "spawned_by": "ADMIN_ROLLUP_MERGE",
                                        "base_branch": base_branch,
                                    },
                                )
                                session.add(history)
                                await session.commit()
                                await broadcast_to_project(
                                    str(task.project_id),
                                    "task_updated",
                                    {
                                        "task_id": str(smoketest_task.id),
                                        "status": TaskStatus.QUEUED,
                                    },
                                )
                                logger.info(
                                    f"[SMOKETEST] Spawned smoketest task {smoketest_task.id} after admin rollup merge to {base_branch}"
                                )
                            except Exception as e:  # noqa: BLE001
                                logger.warning(
                                    f"[SMOKETEST] Failed to spawn smoketest after admin rollup merge: {e}",
                                    exc_info=True,
                                )
                    else:
                        changes["merge_error"] = merge_result.get("message", "unknown")
                        task.latest_status_message = f"Approved but rollup merge failed: {merge_result.get('message', 'unknown')}"
                        logger.error(
                            f"[ADMIN] Approved rollup merge of PR #{rollup_pr_number} failed: {merge_result.get('message')}"
                        )
                except Exception as e:
                    changes["merge_error"] = str(e)[:200]
                    task.latest_status_message = (
                        f"Approved but rollup merge failed: {str(e)[:200]}"
                    )
                    logger.error(
                        f"[ADMIN] Approved rollup merge failed: {e}", exc_info=True
                    )
            else:
                artifact = (
                    (
                        await session.execute(
                            select(Artifact).where(
                                Artifact.task_id == task.id,
                                Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if artifact and artifact.pr_url:
                    pr_number = int(artifact.pr_url.rstrip("/").split("/")[-1])
                    task_payload = task.first_payload
                    _project = await session.get(Project, task.project_id)
                    branch = _derive_task_branch(
                        task.id,
                        task_payload.get("type"),
                        _project.task_branch_namespace,
                    )
                    base_branch = task_payload.get("base_branch", "main")

                    if ctx.get("touched_critical"):
                        # Critical file approval: review already passed, merge the PR
                        try:
                            from app.routers.agent import get_github_token_for_task
                            from app.github import (
                                merge_pull_request,
                                create_pr_comment,
                                delete_branch,
                            )

                            gh_token, owner, repo = await get_github_token_for_task(
                                task.id, session
                            )
                            merge_result = await merge_pull_request(
                                gh_token, owner, repo, pr_number
                            )
                            if merge_result.get("merged"):
                                task.status = TaskStatus.COMPLETED
                                task.context = {**ctx, "pr_merged": True}
                                task.latest_status_message = (
                                    f"Human-approved merge of PR #{pr_number}"
                                )
                                await create_pr_comment(
                                    gh_token,
                                    owner,
                                    repo,
                                    pr_number,
                                    "**Blueprint Review Pipeline**: Human-approved merge (security-critical files).",
                                )
                                try:
                                    await delete_branch(gh_token, owner, repo, branch)
                                except Exception:
                                    logger.exception(
                                        f"[ADMIN] Failed to delete branch {branch} after merge"
                                    )
                                changes["merged_pr"] = pr_number
                                logger.info(
                                    f"[ADMIN] Human-approved merge of PR #{pr_number} (critical files)"
                                )
                            else:
                                changes["merge_error"] = merge_result.get(
                                    "message", "unknown"
                                )
                                task.latest_status_message = f"Approved but merge failed: {merge_result.get('message', 'unknown')}"
                                logger.error(
                                    f"[ADMIN] Approved merge of PR #{pr_number} failed: {merge_result.get('message')}"
                                )
                        except Exception as e:
                            changes["merge_error"] = str(e)[:200]
                            task.latest_status_message = (
                                f"Approved but merge failed: {str(e)[:200]}"
                            )
                            logger.error(
                                f"[ADMIN] Approved merge failed: {e}", exc_info=True
                            )
                    else:
                        # Security scan approval: review hasn't happened yet, spawn it
                        from app.review_pipeline import _spawn_pipeline_task

                        original_prompt = task_payload.get(
                            "prompt", task.description or ""
                        )
                        review_task, _was_created = await _spawn_pipeline_task(
                            task,
                            "review",
                            session,
                            pr_number,
                            branch,
                            base_branch,
                            original_prompt=original_prompt,
                            original_task_id=str(task.id),
                        )
                        if review_task:
                            changes["spawned_review"] = str(review_task.id)
                        else:
                            # Iteration cap reached — revert requires_approval and return error
                            task.requires_approval = True
                            task.status = TaskStatus.FAILED
                            if task.vm_provisioned_at:
                                task.vm_stopped_at = _utcnow()
                            task.latest_status_message = (
                                "Iteration cap reached — no follow-on task spawned"
                            )
                            from app.failure_classification import (
                                persist_failure_category,
                            )

                            persist_failure_category(task, "AGENT_TIMEOUT")
                            _record_failure_result(task, "AGENT_TIMEOUT")
                            session.add(task)
                            await session.commit()
                            raise HTTPException(
                                status_code=422,
                                detail="Iteration cap reached — cannot spawn follow-on task. Archive this task or reset iteration count.",
                            )
                    # Task stays COMPLETED regardless
                    if (
                        "status" in payload
                        and TaskStatus(payload["status"]) == TaskStatus.QUEUED
                    ):
                        task.status = TaskStatus.COMPLETED
                        changes["status"] = TaskStatus.COMPLETED

    if "payloads" in payload:
        task.payloads = payload["payloads"]
        changes["payloads"] = "updated"

    if "parent_task_id" in payload:
        task.parent_task_id = (
            uuid.UUID(payload["parent_task_id"]) if payload["parent_task_id"] else None
        )
        changes["parent_task_id"] = str(task.parent_task_id)

    if "context" in payload and isinstance(payload["context"], dict):
        merged = {**(task.context or {}), **payload["context"]}
        if len(json.dumps(merged)) > 102400:
            raise HTTPException(
                status_code=400, detail="Merged context exceeds 100KB limit"
            )
        task.context = merged
        changes["context"] = "updated"

    # Operator escape hatch for tasks stuck in the post_commit_pending sweep
    # loop. The sweep retries `_process_post_commit` for terminal tasks whose
    # earlier handler chain failed; if `_process_post_commit` can never lock
    # the row (peer holds it across slow ops) AND the bump-attempts session
    # also can't, the sweep gets stuck on this task indefinitely (cycle stall
    # → /health 503 → instance kill cascade). Setting `post_commit_pending:
    # false` here drops the task out of the sweep filter; we also strip the
    # `_post_commit_pending_meta` retry log from context so the row is clean.
    # `post_commit_failed` is set so observers can distinguish "operator-cleared"
    # from "naturally completed".
    if (
        "post_commit_pending" in payload
        and payload["post_commit_pending"] is False
        and task.post_commit_pending
    ):
        task.post_commit_pending = False
        task.post_commit_failed = True
        new_ctx = dict(task.context or {})
        stripped_meta = new_ctx.pop("_post_commit_pending_meta", None)
        new_ctx["_post_commit_abandoned"] = {
            "at": _utcnow().isoformat(),
            "by": "ADMIN_PATCH",
            "prior_attempts": (stripped_meta or {}).get("attempts"),
            "prior_reason": (stripped_meta or {}).get("reason"),
        }
        task.context = new_ctx
        flag_modified(task, "context")
        changes["post_commit_pending"] = False
        logger.info(
            f"[ADMIN] Cleared post_commit_pending on task {task.id} via PATCH "
            f"(prior_attempts={(stripped_meta or {}).get('attempts')})"
        )

    if "depends_on" in payload:
        new_deps = (
            payload["depends_on"] if isinstance(payload["depends_on"], list) else []
        )
        # Validate: all dependency UUIDs must exist and not create cycles
        if new_deps:
            try:
                dep_uuids = [uuid.UUID(d) for d in new_deps]
            except (ValueError, AttributeError):
                raise HTTPException(
                    status_code=400, detail="Invalid UUID in depends_on"
                )
            # Check all referenced tasks exist
            existing = (
                (await session.execute(select(Task.id).where(Task.id.in_(dep_uuids))))
                .scalars()
                .all()
            )
            if len(existing) != len(dep_uuids):
                raise HTTPException(
                    status_code=400,
                    detail="One or more depends_on task IDs do not exist",
                )
            # Cycle detection: walk depends_on graph from each dep to see if we reach task.id
            task_id_str = str(task.id)

            task_cache: dict[str, Task | None] = {}

            async def _has_path_to(
                start_id: str, target_id: str, visited: set, depth: int = 0
            ) -> bool:
                if depth > 50:
                    return False  # DAG too deep, treat as no cycle
                if start_id == target_id:
                    return True
                if start_id in visited:
                    return False
                visited.add(start_id)
                if start_id not in task_cache:
                    task_cache[start_id] = await session.get(Task, uuid.UUID(start_id))
                dep_task = task_cache[start_id]
                if not dep_task:
                    return False
                for d in dep_task.depends_on or []:
                    if await _has_path_to(d, target_id, visited, depth + 1):
                        return True
                return False

            for dep_id in new_deps:
                if await _has_path_to(dep_id, task_id_str, set()):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Circular dependency detected: {dep_id} eventually depends on {task_id_str}",
                    )
        task.depends_on = new_deps
        changes["depends_on"] = task.depends_on

    if changes:
        session.add(task)

        history = TaskHistory(
            task_id=task.id,
            action="MODIFIED",
            details={"changes": changes, "modified_by": "USER"},
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
        from app.rollup_management import _check_and_trigger_rollup, _cleanup_branches

        should_check_rollup = False
        if "status" in payload and task.parent_task_id:
            new_status = TaskStatus(payload["status"])
            if new_status in (
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            ):
                should_check_rollup = True
            # Clean up staging branches when parent task is cancelled/failed
            if new_status in (TaskStatus.CANCELLED, TaskStatus.FAILED):
                ctx = task.context or {}
                if ctx.get("plan_branch") or ctx.get("research_branch"):
                    await _cleanup_branches(task, session)
        if (
            "context" in payload
            and task.parent_task_id
            and isinstance(payload["context"], dict)
            and payload["context"].get("pr_merged")
        ):
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


class SelfAuditRequest(BaseModel):
    """Optional body for POST /projects/{project_id}/self-audit.

    Both fields are optional — when omitted, the agent picks up to ``max_systems``
    systems in round-robin order from the project design_doc.
    """

    focus_systems: List[str] | None = Field(default=None, max_length=20)
    max_systems: int | None = Field(default=None, ge=1, le=10)


@router.post("/projects/{project_id}/self-audit")
async def trigger_self_audit(
    project_id: uuid.UUID,
    body: SelfAuditRequest | None = None,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Enqueue a self_audit task for this project.

    Members and above can trigger a maturity audit. Returns the new task_id.
    """
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    from app.shared_state import runtime_config as _runtime_config

    payload_dict: dict = {
        "type": "self_audit",
        "base_branch": _runtime_config.get("integration_branch", "main"),
    }
    if body is not None:
        if body.focus_systems is not None:
            payload_dict["focus_systems"] = [
                s for s in body.focus_systems if isinstance(s, str) and s.strip()
            ]
        if body.max_systems is not None:
            payload_dict["max_systems"] = body.max_systems
    validated_payload = validate_internal_payload(payload_dict, "admin:self_audit")

    title_suffix = ""
    if body and body.focus_systems:
        title_suffix = f" ({', '.join(body.focus_systems[:3])}{'...' if len(body.focus_systems) > 3 else ''})"

    new_task = Task(
        project_id=project.id,
        title=f"Self audit{title_suffix}"[:200],
        description="Self maturity audit triggered manually",
        payloads=[validated_payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="USER",
        requires_approval=False,
    )
    session.add(new_task)
    await session.commit()
    await session.refresh(new_task)

    history = TaskHistory(
        task_id=new_task.id,
        action="CREATED",
        details={"created_by": "USER", "method": "self_audit_manual"},
    )
    session.add(history)
    await session.commit()

    return {
        "task_id": str(new_task.id),
        "project_id": str(project.id),
        "status": new_task.status,
    }


@router.post("/projects/{project_id}/qualify")
async def trigger_qualify(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Manually trigger the release qualifier for this project's open version.

    Members and above can trigger qualification on demand (used during the
    Phase 4 dry-run calibration window).  Resolves the current open
    ``ReleaseVersion``, runs ``release_qualifier.run_qualification``, and
    returns its result dict.  Returns 404 when there is no open version.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    open_version = await release_version.current_open_version(session, project_id)
    if open_version is None:
        raise HTTPException(
            status_code=404,
            detail="No open release version found for this project.",
        )

    result = await release_qualifier.run_qualification(
        session, project_id, open_version
    )
    await session.commit()
    return result


@router.post("/projects/{project_id}/clear-wave-deps")
async def clear_wave_deps(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Clear depends_on on all QUEUED tasks in a project to unblock wave-gated tasks."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    result = await session.execute(
        select(Task).where(
            Task.project_id == project_id, Task.status == TaskStatus.QUEUED
        )
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
    current_user: dict = Depends(get_current_user),
):
    """Manually trigger rollup PR for an EVALUATING parent task."""
    from app.routers.agent import get_github_token_for_task
    from app.review_pipeline import _spawn_pipeline_task
    from app.github import create_pull_request, merge_pull_request, delete_branch

    task = await _get_user_task_with_role(
        task_id, current_user["user_id"], session, "member"
    )
    if task.status != TaskStatus.EVALUATING:
        raise HTTPException(
            status_code=400, detail=f"Task is {task.status}, not EVALUATING"
        )

    ctx = task.context or {}
    staging_branch = ctx.get("plan_branch") or ctx.get("research_branch")
    target_branch = ctx.get("parent_base") or ctx.get("original_base")

    if not staging_branch or not target_branch:
        raise HTTPException(
            status_code=400, detail="Task has no staging/target branch info in context"
        )

    gh_token, owner, repo = await get_github_token_for_task(task.id, session)

    # Count children
    from sqlmodel import select

    children = (
        (await session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )

    from app.rollup_management import _build_rollup_pr_body

    pr_title = f"[Blueprint] Rollup: {task.title}"
    _dir_id = task.first_payload.get("current_direction_id") or ctx.get("direction_id")
    _dir_title = ctx.get("direction_title") or task.title
    pr_body = _build_rollup_pr_body(
        len(children),
        staging_branch,
        direction_id=str(_dir_id) if _dir_id else None,
        direction_title=_dir_title,
        children=list(children),
    )

    pr_result = await create_pull_request(
        gh_token,
        owner,
        repo,
        title=pr_title,
        head=staging_branch,
        base=target_branch,
        body=pr_body,
    )
    rollup_pr_url = pr_result.get("html_url", "")
    rollup_pr_number = pr_result.get("number")

    is_target_intermediate = target_branch.startswith(
        "research/"
    ) or target_branch.startswith("plan/")

    if is_target_intermediate:
        merge_result = await merge_pull_request(gh_token, owner, repo, rollup_pr_number)
        if merge_result.get("merged"):
            task.status = TaskStatus.COMPLETED
            task.context = {**ctx, "pr_merged": True, "rollup_pr_url": rollup_pr_url}
            task.latest_status_message = f"Rollup PR auto-merged into {target_branch}"
            await delete_branch(gh_token, owner, repo, staging_branch)
        else:
            task.latest_status_message = (
                f"Rollup merge failed: {merge_result.get('message')}"
            )
    else:
        task.context = {
            **ctx,
            "rollup_pr_url": rollup_pr_url,
            "rollup_pr_number": rollup_pr_number,
        }
        review_task, _ = await _spawn_pipeline_task(
            task,
            "review",
            session,
            rollup_pr_number,
            staging_branch,
            target_branch,
            original_prompt=task.description or task.title,
            original_task_id=str(task.id),
        )
        if review_task:
            task.latest_status_message = f"Rollup PR #{rollup_pr_number} created, review spawned → {review_task.id}"
        else:
            # _spawn_pipeline_task sets source_task.latest_status_message to a
            # specific reason (already merged / iteration cap / deferred behind
            # in-flight task / stuck cycle) and writes a SPAWN_REASON code into
            # context so callers can distinguish recoverable from terminal cases.
            from app.review_pipeline import (
                SPAWN_REASON_KEY,
                SPAWN_REASON_DEFERRED,
                SPAWN_REASON_DEDUP,
                SPAWN_REASON_ALREADY_MERGED,
            )

            spawn_reason_code = (task.context or {}).get(SPAWN_REASON_KEY)
            spawn_reason_msg = task.latest_status_message
            if spawn_reason_code == SPAWN_REASON_ALREADY_MERGED:
                # PR was already merged before review could spawn — terminal success.
                task.status = TaskStatus.COMPLETED
                task.context = {**(task.context or {}), "pr_merged": True}
                task.latest_status_message = (
                    f"Rollup PR #{rollup_pr_number} already merged"
                )
            elif spawn_reason_code in (SPAWN_REASON_DEFERRED, SPAWN_REASON_DEDUP):
                # Recoverable: a sibling task is in-flight on the same PR. Keep
                # the parent EVALUATING so the orchestrator retries review-spawn
                # once the in-flight task completes.
                task.latest_status_message = f"Rollup PR #{rollup_pr_number} created — review deferred ({spawn_reason_msg})"
            else:
                # Terminal failure (iteration cap, stuck cycle, blocked repeat,
                # or unknown). Operator must intervene.
                task.status = TaskStatus.FAILED
                if task.vm_provisioned_at:
                    task.vm_stopped_at = _utcnow()
                task.latest_status_message = (
                    f"Rollup PR #{rollup_pr_number} created but review not spawned — {spawn_reason_msg}"
                    if spawn_reason_msg
                    else f"Rollup PR #{rollup_pr_number} created but review not spawned (unknown reason)"
                )
                from app.failure_classification import persist_failure_category

                persist_failure_category(task, "AGENT_TIMEOUT")
                _record_failure_result(task, "AGENT_TIMEOUT")

    session.add(task)
    await session.commit()

    return {
        "rollup_pr_url": rollup_pr_url,
        "rollup_pr_number": rollup_pr_number,
        "target": target_branch,
        "status": task.status,
    }


@router.get("/projects/{project_id}/findings")
async def get_project_findings(
    project_id: uuid.UUID,
    direction_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return structured research findings for a project, with claims and sources.

    Optional direction_id filter scopes results to a single direction.
    Ordered newest-task-first; bounded by days recency and limit total rows.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )
    from app.research_completion import query_findings_index

    rows = await query_findings_index(
        session, project_id, direction_id=direction_id, limit=limit, days=days
    )
    return {"findings": rows, "count": len(rows)}


# --- Deployment State & Health ---


def _get_zone_health_summary() -> dict:
    try:
        from app.compute import get_zone_health_summary

        return get_zone_health_summary()
    except Exception:  # noqa: BLE001
        return {"total_zones": 0, "available": 0, "exhausted": []}


async def build_deploy_state_payload(session: AsyncSession) -> dict:
    """Assemble the deploy state payload. Used by both HTTP endpoint and WS broadcasts."""
    from app.shared_state import deploy_monitoring, external_health, runtime_config
    import app.deploy_health as _deploy_health_module

    state = await session.get(DeploymentState, 1)
    if not state:
        return {
            "error": "No deployment state found — service may not have initialized yet"
        }

    # Calculate monitoring window progress
    window_progress = None
    if deploy_monitoring["window_start"]:
        duration = (
            deploy_monitoring["extended_window"]
            if deploy_monitoring["is_extended"]
            else deploy_monitoring["window_duration"]
        )
        elapsed = (_utcnow() - deploy_monitoring["window_start"]).total_seconds()
        window_progress = {
            "elapsed_seconds": int(elapsed),
            "total_seconds": duration,
            "percent": min(100, int(elapsed / duration * 100)),
        }

    # Calculate recent failure rate
    results = deploy_monitoring["task_results"]
    failure_rate = 0.0
    if results:
        failures = sum(1 for r in results if r[1] == "failure")
        failure_rate = failures / len(results)

    current_vms = runtime_config["max_concurrent_vms"]
    target_vms = state.runtime_max_concurrent_vms
    ramp_in_progress = (
        state.status in (DeploymentStatus.MONITORING, DeploymentStatus.STABLE)
        and state.pre_pause_max_vms is None
        and target_vms is not None
        and 0 < current_vms < target_vms
    )
    ramp_step_size = min(2, target_vms - current_vms) if ramp_in_progress else 0

    # external_health holds a live datetime in last_check; copy and coerce so
    # WebSocket consumers using send_json (which lacks FastAPI's encoder)
    # don't choke on "Object of type datetime is not JSON serializable".
    _last_check = external_health.get("last_check")
    external_health_serializable = {
        **external_health,
        "last_check": _last_check.isoformat()
        if isinstance(_last_check, datetime)
        else _last_check,
    }

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
        "reason": state.pause_reason
        if state.status == DeploymentStatus.PAUSED
        else None,
        "pre_pause_max_vms": state.pre_pause_max_vms,
        "monitoring_window": window_progress,
        "failure_rate": round(failure_rate, 3),
        "task_results_count": len(results),
        "external_health": external_health_serializable,
        "max_concurrent_vms": runtime_config["max_concurrent_vms"],
        "current_vms": current_vms,
        "target_vms": target_vms,
        "ramp_in_progress": ramp_in_progress,
        "ramp_step_size": ramp_step_size,
        "zone_health": _get_zone_health_summary(),
        "gcp_project_id": os.getenv("GCP_PROJECT_ID", ""),
        "health_recovery_streak": sum(
            1
            for x in getattr(_deploy_health_module, "_health_recovery_history", [])
            if x
        ),
        "health_recovery_streak_required": 3,
        "health_recovery_history": list(
            getattr(_deploy_health_module, "_health_recovery_history", [])
        ),
        "health_recovery_history_size": 5,
        "health_failure_streak": getattr(
            _deploy_health_module, "_pause_failure_streak", 0
        ),
        "health_failure_streak_threshold": 3,
        "health_check_interval_seconds": 15
        if runtime_config["max_concurrent_vms"] == 0
        else 60,
        "max_auto_pause_duration_seconds": runtime_config.get(
            "max_auto_pause_duration_seconds", 7200
        ),
    }
