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

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import get_session
from app.message_convergence import get_message_link, resolve_message_ref
from app.models import Project, QualificationVerdict, ReleaseVersion, Task
from app.routers.agent import verify_agent_token
from app.shared_state import limiter
from app.source_ref_validation import (
    ground_design_doc_value,
    resolve_branch_verification_context,
)

router = APIRouter(prefix="/api/agent", tags=["agent"])

logger = logging.getLogger(__name__)

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


@router.get("/projects/{project_id}/design-doc/{key:path}")
@limiter.limit("60/minute")
async def agent_fetch_design_doc_section(
    request: Request,
    project_id: uuid.UUID,
    key: str,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return a single design_doc section by key with its FULL content. Used
    when the strategic payload ships design_doc as a compact index (one entry
    per section: key + summary + reconciled status) and the agent needs the
    full prose/specs of a specific section before acting on it.

    The returned section is presence-grounded against the project's real
    integration branch via the SAME ``ground_design_doc_value`` path as the
    write-capable design-doc ``get`` op (admin_agent_ops.py), so the fetched
    status matches what the index already showed and the agent never sees an
    un-reconciled status. Cross-project access and missing keys both 404."""
    project = await _resolve_agent_project(project_id, token, session)
    doc = project.design_doc or {}
    value = doc.get(key)
    if value is None:
        raise HTTPException(status_code=404, detail="Design doc section not found")
    verification_kwargs = await resolve_branch_verification_context(session, project)
    value = await ground_design_doc_value(value, key, **verification_kwargs)
    return {"status": "success", "data": {"key": key, "value": value}}


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


@router.get("/projects/{project_id}/messages/{ref}")
@limiter.limit("60/minute")
async def agent_fetch_message(
    request: Request,
    project_id: uuid.UUID,
    ref: str,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Resolve a project message by short ref (first 8+ hex chars of its UUID)
    or full UUID and return its content. Used when a message has aged out of
    the live strategic thread (only status=open messages are loaded into the
    spawn context) and the agent needs to inspect it before acting on it.

    Project-scoped via ``_resolve_agent_project`` (cross-project → 404). The
    shared resolver fails loudly on ambiguous (409), missing (404), and
    too-short (400) refs, and matches across all statuses."""
    project = await _resolve_agent_project(project_id, token, session)
    msg = await resolve_message_ref(session, project.id, ref)
    message_type = msg.message_type
    type_value = message_type.value if hasattr(message_type, "value") else str(message_type)
    return {
        "status": "success",
        "data": {
            "id": str(msg.id),
            "content": msg.content,
            "type": type_value,
            "from_type": msg.from_type,
            "status": msg.status,
            "resolution": msg.resolution,
            "convergence_link": get_message_link(msg),
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
        },
    }


@router.get("/projects/{project_id}/qualification")
@limiter.limit("60/minute")
async def agent_fetch_qualification(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return the latest qualification verdict context for the project's open version.

    Read-only; agent-scoped. Prefers the durable per-attempt evidence on the
    latest immutable QualificationVerdict row (its audit_findings column survives
    re-qualification overwrites), falling back to the mutable
    last_qualification blob for legacy rows. The source field reflects which
    store was used. Returns {"source": "no_open_version"} when no open version
    exists or no qualification has run yet.

    Payload includes: candidate_sha, finalized_at, status, confidence,
    sub_scores, findings, excluded_findings, challenger_refuted_findings.
    """
    from app.release_qualifier import (  # noqa: PLC0415
        get_latest_qualification_audit_findings,
    )

    await _resolve_agent_project(project_id, token, session)

    open_row = (
        (
            await session.execute(
                select(ReleaseVersion)
                .where(
                    ReleaseVersion.project_id == project_id,
                    ReleaseVersion.status == "open",
                )
                .order_by(ReleaseVersion.opened_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if open_row is None:
        return {"source": "no_open_version"}

    # Re-qualification readiness signal + per-finding resolution view (ADDITIVE).
    # The fix-first triage workflow needs to know, from the same read it already
    # makes, whether every gating high+medium carried finding is resolved-or-stale
    # (the pre-registered re-qualification trigger) and the per-finding ledger
    # state for each. Best-effort: a failure to compute the ledger-backed signal
    # must never break this read or change the existing response shape, so it
    # degrades to ``requalification_ready: None`` + an empty resolution view.
    requalification_extra: dict = {
        "requalification_ready": None,
        "finding_resolution": [],
    }
    try:
        from app.release_readiness import (  # noqa: PLC0415
            compute_requalification_readiness,
        )

        readiness = await compute_requalification_readiness(session, project_id)
        requalification_extra = {
            "requalification_ready": readiness["requalification_ready"],
            # Per-finding resolution view: each gating carried finding's key,
            # severity, summary, and resolution state (resolved|stale|unresolved).
            "finding_resolution": readiness["findings"],
        }
    except Exception:
        logger.exception(
            "[AGENT_FETCH] requalification readiness compute failed for project %s; "
            "serving qualification without the readiness signal",
            project_id,
        )

    verdict = (
        (
            await session.execute(
                select(QualificationVerdict)
                .where(
                    QualificationVerdict.release_version_id == open_row.id,
                    QualificationVerdict.audit_findings.is_not(None),
                )
                .order_by(QualificationVerdict.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )

    if verdict is not None:
        return {
            "candidate_sha": verdict.candidate_sha,
            "finalized_at": verdict.finalized_at.isoformat() if verdict.finalized_at else None,
            "status": verdict.terminal_status,
            "confidence": verdict.confidence,
            "sub_scores": verdict.sub_scores,
            "findings": get_latest_qualification_audit_findings(open_row, verdict),
            "excluded_findings": verdict.excluded_audit_findings or [],
            "challenger_refuted_findings": verdict.challenger_refuted_audit_findings or [],
            "source": "qualification_verdict",
            **requalification_extra,
        }

    qual = open_row.last_qualification or {}
    return {
        "candidate_sha": qual.get("candidate_sha"),
        "finalized_at": qual.get("finalized_at"),
        "status": qual.get("status"),
        "confidence": qual.get("confidence"),
        "sub_scores": qual.get("sub_scores"),
        "findings": get_latest_qualification_audit_findings(open_row),
        "excluded_findings": qual.get("excluded_audit_findings") or [],
        "challenger_refuted_findings": qual.get("challenger_refuted_audit_findings") or [],
        "source": "last_qualification",
        **requalification_extra,
    }


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
