"""Agent-facing strategic-context fetch endpoints.

Extracted from app.routers.agent to give the read-only strategic
direction/playbook/gap fetch surface its own seam, so it can evolve
independently of the agent lifecycle (claim/heartbeat/GitHub) flow.

The strategic-spawn payload is now packed under a tight budget (see
pack_under_budget in payload_schemas.py). When the packer trims a list or
drops a section entirely, the agent sees that in payload._packer_meta and
can call these endpoints to pull the full detail it needs. Deliberately
scoped to read-only project state — mutations stay on the existing
/api/agent/strategic POST surface.

Auth is shared with the main agent router via ``verify_agent_token``; the
router is registered under the same ``/api/agent`` prefix in app.main so
the route paths stay byte-identical and the agent-side fetch script needs
no change.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import Project, Task
from app.routers.agent import verify_agent_token
from app.shared_state import limiter

router = APIRouter(prefix="/api/agent", tags=["agent"])

_RESEARCH_HISTORY_KEEP_ON_FETCH = 20


async def _resolve_agent_project(
    project_id: uuid.UUID,
    token: dict,
    session: AsyncSession,
) -> Project:
    """Confirm the agent's task belongs to *project_id* and return the project.

    The agent JWT carries the task id (``sub`` claim) but not the project id,
    so we look up the task and verify the path matches. Cross-project access
    returns 404 (not 403) so probing for other projects' state can't be used
    to enumerate ids.
    """
    task_id = token.get("sub")
    if not task_id:
        raise HTTPException(status_code=401, detail="Missing task id in token")
    task = await session.get(Task, uuid.UUID(task_id))
    if task is None or task.project_id != project_id:
        raise HTTPException(status_code=404, detail="Project not found")
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/projects/{project_id}/directions/{direction_id}")
@limiter.limit("60/minute")
async def agent_fetch_direction(
    request: Request,
    project_id: uuid.UUID,
    direction_id: str,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return one direction by id, with research_history capped at the most
    recent 20 cycles. Used by the agent when ``directions_summary`` was
    list_trimmed and it needs deeper detail on a specific direction."""
    project = await _resolve_agent_project(project_id, token, session)
    direction = next(
        (d for d in (project.directions or []) if d.get("id") == direction_id),
        None,
    )
    if direction is None:
        raise HTTPException(status_code=404, detail="Direction not found")
    out = dict(direction)
    history = out.get("research_history")
    if isinstance(history, list) and len(history) > _RESEARCH_HISTORY_KEEP_ON_FETCH:
        out["research_history"] = history[-_RESEARCH_HISTORY_KEEP_ON_FETCH:]
        out["_history_truncated"] = {
            "shown": _RESEARCH_HISTORY_KEEP_ON_FETCH,
            "total": len(history),
        }
    return {"status": "success", "data": out}


@router.get("/projects/{project_id}/playbook/{key:path}")
@limiter.limit("60/minute")
async def agent_fetch_playbook_entry(
    request: Request,
    project_id: uuid.UUID,
    key: str,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return a single ``playbook/<key>`` entry from design_doc with full
    content. Used when ``playbook_summary`` was dropped from the strategic
    payload but the agent needs to consult a specific entry."""
    project = await _resolve_agent_project(project_id, token, session)
    my_task_id = token.get("sub") or ""
    design_doc = project.design_doc or {}
    full_key = f"playbook/{key}" if not key.startswith("playbook/") else key
    entry = design_doc.get(full_key)
    if entry is None:
        raise HTTPException(status_code=404, detail="Playbook entry not found")
    # Enforce pending_source visibility: hide entries written by other in-flight
    # tasks (same rule as claim-time loader and /api/agent/playbook get/list).
    if isinstance(entry, dict):
        pending_source = entry.get("pending_source")
        if pending_source and pending_source != my_task_id:
            raise HTTPException(status_code=404, detail="Playbook entry not found")
    return {"status": "success", "data": {"key": full_key, "entry": entry}}


@router.get("/projects/{project_id}/problem_model")
@router.get("/projects/{project_id}/problem-model")
@limiter.limit("60/minute")
async def agent_fetch_problem_model(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return the FULL untrimmed problem_model. Used when the strategic
    payload trimmed (per-gap arrays capped, archived_gaps dropped) or dropped
    problem_model under budget pressure and the agent wants the complete
    current state before writing — writes merge non-destructively, so this is
    for reading the whole record, not a precondition for a safe write.

    Both the underscore (``/problem_model``) and hyphen (``/problem-model``)
    spellings are served as aliases: ``main`` shipped the underscore route
    (#7449) and ``automain`` shipped the hyphen route (#7451). Keeping both
    means agents from either baked image can fetch the model during the
    image-rollover window."""
    project = await _resolve_agent_project(project_id, token, session)
    return {"status": "success", "data": project.problem_model or {}}


@router.get("/projects/{project_id}/gaps/{gap_id}")
@limiter.limit("60/minute")
async def agent_fetch_gap(
    request: Request,
    project_id: uuid.UUID,
    gap_id: str,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return a single problem_model gap (active or archived) with full
    history/past_moves/evidence. Used when problem_model was summarized
    (per-gap arrays trimmed) and the agent needs the full lineage of a
    specific gap to reason about it."""
    project = await _resolve_agent_project(project_id, token, session)
    pm = project.problem_model or {}
    for source_key, archived_flag in (("gaps", False), ("archived_gaps", True)):
        for g in pm.get(source_key) or []:
            if isinstance(g, dict) and g.get("id") == gap_id:
                return {
                    "status": "success",
                    "data": g,
                    "from_archive": archived_flag,
                }
    raise HTTPException(status_code=404, detail="Gap not found")
