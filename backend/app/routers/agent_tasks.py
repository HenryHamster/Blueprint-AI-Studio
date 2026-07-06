"""Agent-facing task-status fetch endpoints.

Lets the strategic agent inspect the status and outcome of tasks it spawned
via spawn_task/spawn_fix, closing the zero-visibility gap that existed after
spawning: the agent had no way to tell if a task was queued, running, blocked,
completed, or failed.

Auth/scoping follows the same ``_resolve_agent_project`` pattern used in
``agent_strategic_fetch``: the agent JWT carries the calling task-id, and we
confirm the calling task belongs to the requested project (cross-project access
returns 404, not 403, to prevent project enumeration).

Only tasks with ``created_by == 'STRATEGIC_AGENT'`` are visible through this
surface. The response is a compact, agent-safe subset of the admin task detail:
no cost, no billing, no other users' data.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import get_session
from app.models import Artifact, ArtifactType, Task, TaskStatus
from app.routers.agent import verify_agent_token
from app.routers.agent_strategic_fetch import _resolve_agent_project
from app.shared_state import limiter

router = APIRouter(prefix="/api/agent", tags=["agent"])

_STRATEGIC_AGENT_CREATED_BY = "STRATEGIC_AGENT"
_LIST_LIMIT_DEFAULT = 20
_LIST_LIMIT_MAX = 100
_ANCESTOR_WALK_MAX_DEPTH = 20

# Terminal statuses that warrant fetching artifact/failure detail.
_TERMINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt else None


def _task_type(task: Task) -> str | None:
    """Extract task_type from first payload."""
    fp = task.first_payload
    return fp.get("type") if isinstance(fp, dict) else None


def _build_task_response(
    task: Task,
    pr_url: str | None = None,
    error_summary: str | None = None,
) -> dict[str, Any]:
    """Build compact agent-visible task status response."""
    status_val = getattr(task.status, "value", str(task.status)) if task.status else None
    resp: dict[str, Any] = {
        "id": str(task.id),
        "status": status_val,
        "task_type": _task_type(task),
        "title": task.title,
        "created_at": _iso(task.created_at),
        "updated_at": _iso(task.updated_at),
        "latest_status_message": task.latest_status_message,
    }

    # Blocked / parked detail
    if task.status == TaskStatus.BLOCKED:
        resp["blocked_reason"] = task.blocked_reason
        resp["blocker_kind"] = task.blocker_kind
    if task.status == TaskStatus.PARKED:
        resp["park_reason"] = task.park_reason

    # Outcome detail for terminal tasks
    if task.status in _TERMINAL_STATUSES:
        resp["completed_at"] = _iso(task.completed_at)
        if pr_url:
            resp["pr_url"] = pr_url
        if error_summary:
            resp["failure_reason"] = error_summary

    return resp


@router.get("/projects/{project_id}/tasks/{task_id}")
@limiter.limit("60/minute")
async def agent_get_task(
    request: Request,
    project_id: uuid.UUID,
    task_id: str,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
) -> dict[str, Any]:
    """Return status and outcome for a single STRATEGIC_AGENT-spawned task.

    Returns 404 when the task does not exist, does not belong to this project,
    or was not created by the strategic agent (prevents cross-type enumeration).
    """
    await _resolve_agent_project(project_id, token, session)

    try:
        tid = uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Task not found")

    task = await session.get(Task, tid)
    if task is None or task.project_id != project_id:
        raise HTTPException(status_code=404, detail="Task not found")

    # Allow visibility when the task itself is strategist-authored, OR any
    # ancestor reachable via parent_task_id (within this project) is.
    if task.created_by != _STRATEGIC_AGENT_CREATED_BY:
        current = task
        strategist_ancestor_found = False
        for _ in range(_ANCESTOR_WALK_MAX_DEPTH):
            if current.parent_task_id is None:
                break
            parent = await session.get(Task, current.parent_task_id)
            if parent is None or parent.project_id != project_id:
                break
            if parent.created_by == _STRATEGIC_AGENT_CREATED_BY:
                strategist_ancestor_found = True
                break
            current = parent
        if not strategist_ancestor_found:
            raise HTTPException(status_code=404, detail="Task not found")

    pr_url: str | None = None
    error_summary: str | None = None

    if task.status in _TERMINAL_STATUSES:
        # Fetch PR artifact and error summary only for terminal tasks.
        artifact_rows = (
            await session.execute(
                select(Artifact)
                .where(
                    Artifact.task_id == tid,
                    Artifact.artifact_type.in_(
                        [ArtifactType.PULL_REQUEST, ArtifactType.ERROR_LOG]
                    ),
                )
                .order_by(Artifact.created_at.asc())
            )
        ).scalars().all()

        for a in artifact_rows:
            if a.artifact_type == ArtifactType.PULL_REQUEST and a.pr_url:
                pr_url = a.pr_url
            if a.artifact_type == ArtifactType.ERROR_LOG and a.logs and not error_summary:
                # Surface a compact summary (first 500 chars) rather than full logs.
                error_summary = a.logs[:500]

        # Fall back to latest_status_message as failure reason when no error log.
        if task.status == TaskStatus.FAILED and not error_summary:
            error_summary = task.latest_status_message

    return {"status": "success", "data": _build_task_response(task, pr_url, error_summary)}


@router.get("/projects/{project_id}/tasks")
@limiter.limit("60/minute")
async def agent_list_tasks(
    request: Request,
    project_id: uuid.UUID,
    limit: int = Query(default=_LIST_LIMIT_DEFAULT, ge=1, le=_LIST_LIMIT_MAX),
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
) -> dict[str, Any]:
    """List recent STRATEGIC_AGENT-spawned tasks for a project.

    Returns tasks ordered by ``created_at`` descending (most recent first),
    capped at ``limit`` (default 20, max 100). Only tasks created by the
    strategic agent are visible.
    """
    await _resolve_agent_project(project_id, token, session)

    # Fetch all strategist-authored tasks and their transitive descendants
    # within this project using a single recursive CTE.
    cte_result = await session.execute(
        text("""
            WITH RECURSIVE task_tree AS (
                SELECT id, created_at
                FROM task
                WHERE project_id = :project_id AND created_by = :created_by
                UNION ALL
                SELECT t.id, t.created_at
                FROM task t
                INNER JOIN task_tree tt ON t.parent_task_id = tt.id
                WHERE t.project_id = :project_id
            )
            SELECT id FROM task_tree
            ORDER BY created_at DESC
            LIMIT :limit
        """),
        {
            "project_id": project_id,
            "created_by": _STRATEGIC_AGENT_CREATED_BY,
            "limit": limit,
        },
    )
    task_ids = [row[0] for row in cte_result.fetchall()]

    if not task_ids:
        return {"status": "success", "data": [], "count": 0}

    rows_unordered = (
        await session.execute(select(Task).where(Task.id.in_(task_ids)))
    ).scalars().all()
    # Re-sort to match CTE ordering (IN query does not preserve order).
    id_order = {tid: idx for idx, tid in enumerate(task_ids)}
    rows = sorted(rows_unordered, key=lambda t: id_order.get(t.id, len(task_ids)))

    # Fetch PR artifacts for terminal tasks in one query to avoid N+1.
    terminal_ids = [t.id for t in rows if t.status in _TERMINAL_STATUSES]
    pr_map: dict[uuid.UUID, str] = {}
    if terminal_ids:
        art_rows = (
            await session.execute(
                select(Artifact)
                .where(
                    Artifact.task_id.in_(terminal_ids),
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                    Artifact.pr_url.isnot(None),
                )
                .order_by(Artifact.created_at.asc())
            )
        ).scalars().all()
        for a in art_rows:
            pr_map.setdefault(a.task_id, a.pr_url)  # type: ignore[arg-type]

    items = []
    for t in rows:
        pr_url = pr_map.get(t.id) if t.status in _TERMINAL_STATUSES else None
        items.append(_build_task_response(t, pr_url=pr_url))

    return {"status": "success", "data": items, "count": len(items)}
