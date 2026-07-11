"""Agent-facing task-status fetch endpoints.

Lets the strategic agent inspect the status and outcome of tasks it spawned
via spawn_task/spawn_fix, as well as transitive descendants of those tasks
(children spawned by the orchestrator on behalf of the strategist).

Auth/scoping follows the same ``_resolve_agent_project`` pattern used in
``agent_strategic_fetch``: the agent JWT carries the calling task-id, and we
confirm the calling task belongs to the requested project (cross-project access
returns 404, not 403, to prevent project enumeration). The trust boundary is
PER-PROJECT, not per-agent-role: any active task-scoped agent JWT whose calling
task lives in the project — not only the strategic agent's — passes this gate,
and can therefore list strategist-spawned tasks and read their PR URLs and the
compact ERROR_LOG summary. There is no additional strategist-authorship check on
the *caller*; this matches the same-project trust boundary the rest of the
``agent_*`` fetch surface relies on. (The strategist-lineage filter below
governs which tasks are *returned*, not which agents may call.)

Tasks are visible when (a) created_by == 'STRATEGIC_AGENT', or (b) reachable
via parent_task_id links from a STRATEGIC_AGENT root within the same project.
The recursive CTE is bounded to ``_ANCESTOR_WALK_MAX_DEPTH`` generations so
admin-mutable parent links cannot trigger unbounded recursion. Each response
item includes ``parent_task_id`` so the strategist can map a failing descendant
back to the task it spawned.

The response is a compact, agent-safe subset of the admin task detail:
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
    children: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build compact agent-visible task status response.

    Includes the flat+children contract: each task item carries its direct
    children inline via the children array (empty if no children).
    """
    status_val = getattr(task.status, "value", str(task.status)) if task.status else None
    resp: dict[str, Any] = {
        "id": str(task.id),
        "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
        "status": status_val,
        "task_type": _task_type(task),
        "title": task.title,
        "created_at": _iso(task.created_at),
        "updated_at": _iso(task.updated_at),
        "latest_status_message": task.latest_status_message,
        "children": children if children is not None else [],
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


async def _fetch_children(
    parent_ids: list[uuid.UUID],
    project_id: uuid.UUID,
    session: AsyncSession,
) -> dict[uuid.UUID, list[dict[str, Any]]]:
    """Return direct children of each parent_id, keyed by parent_id.

    Children are scoped to the same project (cross-project isolation).
    No created_by filter — child tasks are spawned by the pipeline, not
    directly by the strategic agent.
    """
    result: dict[uuid.UUID, list[dict[str, Any]]] = {pid: [] for pid in parent_ids}
    if not parent_ids:
        return result

    child_rows = (
        await session.execute(
            select(Task)
            .where(
                Task.parent_task_id.in_(parent_ids),
                Task.project_id == project_id,
            )
            .order_by(Task.created_at.asc())
            .limit(5000)
        )
    ).scalars().all()

    if not child_rows:
        return result

    terminal_child_ids = [c.id for c in child_rows if c.status in _TERMINAL_STATUSES]
    child_pr_map: dict[uuid.UUID, str] = {}
    if terminal_child_ids:
        art_rows = (
            await session.execute(
                select(Artifact)
                .where(
                    Artifact.task_id.in_(terminal_child_ids),
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                    Artifact.pr_url.isnot(None),
                )
                .order_by(Artifact.created_at.asc())
            )
        ).scalars().all()
        for a in art_rows:
            child_pr_map.setdefault(a.task_id, a.pr_url)  # type: ignore[arg-type]

    for child in child_rows:
        pr_url = child_pr_map.get(child.id) if child.status in _TERMINAL_STATUSES else None
        # Children of children are not fetched inline; grandchildren stay at the top level (flat contract)
        child_dict = _build_task_response(child, pr_url=pr_url, children=[])
        if child.parent_task_id in result:
            result[child.parent_task_id].append(child_dict)

    return result


@router.get("/projects/{project_id}/tasks/{task_id}")
@limiter.limit("60/minute")
async def agent_get_task(
    request: Request,
    project_id: uuid.UUID,
    task_id: str,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
) -> dict[str, Any]:
    """Return status and outcome for a single strategist-lineage task in this project.

    Callable by any task-scoped agent JWT whose calling task belongs to
    ``project_id`` — the gate is per-project, not per-agent-role (see the module
    docstring); it is not restricted to the strategic agent. Visible tasks
    include any STRATEGIC_AGENT-authored task and any transitive descendant
    reachable via parent_task_id within the same project (bounded to
    _ANCESTOR_WALK_MAX_DEPTH generations). Returns 404 when the task does not
    exist, does not belong to this project, or has no STRATEGIC_AGENT ancestor
    (prevents cross-type enumeration). Response includes parent_task_id so the
    agent can map descendants back to the task it spawned, plus a ``children``
    list of direct child tasks (spawned by the pipeline under this parent), so
    the strategist can inspect descendant progress.
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
        # Single recursive CTE replaces N+1 per-iteration session.get().
        cte_result = await session.execute(
            text("""
                WITH RECURSIVE ancestor_chain AS (
                    SELECT parent_task_id, created_by, 0 AS depth
                    FROM task
                    WHERE id = :task_id AND parent_task_id IS NOT NULL
                    UNION ALL
                    SELECT t.parent_task_id, t.created_by, ac.depth + 1
                    FROM task t
                    INNER JOIN ancestor_chain ac ON t.id = ac.parent_task_id
                    WHERE ac.parent_task_id IS NOT NULL
                      AND t.project_id = :project_id
                      AND ac.depth < :max_depth
                )
                SELECT 1 FROM ancestor_chain
                WHERE created_by = :created_by
                LIMIT 1
            """),
            {
                "task_id": tid,
                "max_depth": _ANCESTOR_WALK_MAX_DEPTH,
                "created_by": _STRATEGIC_AGENT_CREATED_BY,
                "project_id": project_id,
            },
        )
        if not cte_result.fetchone():
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

    children_map = await _fetch_children([task.id], project_id, session)
    data = _build_task_response(task, pr_url, error_summary, children=children_map.get(task.id, []))
    return {"status": "success", "data": data}


@router.get("/projects/{project_id}/tasks")
@limiter.limit("60/minute")
async def agent_list_tasks(
    request: Request,
    project_id: uuid.UUID,
    limit: int = Query(default=_LIST_LIMIT_DEFAULT, ge=1, le=_LIST_LIMIT_MAX),
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
) -> dict[str, Any]:
    """List recent strategist-lineage tasks for a project.

    Callable by any task-scoped agent JWT whose calling task belongs to
    ``project_id`` — the gate is per-project, not per-agent-role (see the module
    docstring); it is not restricted to the strategic agent.
    Returns STRATEGIC_AGENT-authored tasks and their transitive descendants
    (children spawned by the orchestrator on behalf of the strategist), ordered
    by ``created_at`` descending (most recent first), capped at ``limit``
    (default 20, max 100). The recursive CTE is bounded to
    ``_ANCESTOR_WALK_MAX_DEPTH`` generations. Each item includes
    ``parent_task_id`` (null for strategist roots) so the agent can map
    descendants back to the task it spawned.
    """
    await _resolve_agent_project(project_id, token, session)

    # Fetch all strategist-authored tasks and their transitive descendants
    # within this project using a single recursive CTE, bounded by
    # _ANCESTOR_WALK_MAX_DEPTH to prevent unbounded recursion via
    # admin-mutable parent links.
    cte_result = await session.execute(
        text("""
            WITH RECURSIVE task_tree AS (
                SELECT id, created_at, 0 AS depth
                FROM task
                WHERE project_id = :project_id AND created_by = :created_by
                UNION ALL
                SELECT t.id, t.created_at, tt.depth + 1
                FROM task t
                INNER JOIN task_tree tt ON t.parent_task_id = tt.id
                WHERE t.project_id = :project_id AND tt.depth < :max_depth
            )
            SELECT id FROM task_tree
            ORDER BY created_at DESC
            LIMIT :limit
        """),
        {
            "project_id": project_id,
            "created_by": _STRATEGIC_AGENT_CREATED_BY,
            "limit": limit,
            "max_depth": _ANCESTOR_WALK_MAX_DEPTH,
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

    parent_ids = [t.id for t in rows]
    children_map = await _fetch_children(parent_ids, project_id, session)

    items = []
    for t in rows:
        pr_url = pr_map.get(t.id) if t.status in _TERMINAL_STATUSES else None
        item = _build_task_response(t, pr_url=pr_url, children=children_map.get(t.id, []))
        items.append(item)

    return {"status": "success", "data": items, "count": len(items)}
