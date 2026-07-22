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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import select

from app.database import get_session
from app.direction_outcomes import proven_merged_count, reachable_merged_count, verified_merged_count
from app.knowledge_query import build_compute_sweep_groups, build_sweep_knowledge_index
from app.message_convergence import get_message_link, resolve_message_ref
from app.models import Project, ProjectMessage, QualificationVerdict, ReleaseVersion, Task, UiLoadTimingSample
from app.project_message_projection import agent_fetch_message_dict
from app.qualification_state_resolver import apply_verdict_substitution
from app.release_scoring import blocker_execution_site as _blocker_execution_site
from app.routers.agent import verify_agent_token
from app.shared_state import limiter
from app.source_ref_validation import (
    ground_design_doc_value,
    resolve_branch_verification_context,
)

router = APIRouter(prefix="/api/agent", tags=["agent"])

logger = logging.getLogger(__name__)

_RESEARCH_HISTORY_KEEP_ON_FETCH = 20
_RECENT_CLOSED_VERSIONS_KEEP = 5


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt else None


def _compact_last_qualification(qual: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return lifecycle-safe qualification summary without full audit payloads."""
    if not isinstance(qual, dict) or not qual:
        return None

    keys = (
        "status",
        "qualification_attempt_id",
        "retry_episode",
        "retry_episode_cap",
        "candidate_sha",
        "confidence",
        "threshold",
        "started_at",
        "finalized_at",
        "blocker",
        "execution_site",
        "sub_scores",
        "ci_resolution",
        "audit_missing_reason",
        "would_promote",
        "promotion_pr_url",
        "promotion_conflict",
        "promotion_error_detail",
        "error",
        "detail",
        "goal_alignment",
        "blind_merge_grade",
        "merge_grade_status",
        "merge_grade_coverage",
        "merge_grade_biggest_gap",
        "debt_enforce_confidence_preview",
        "debt_enforce_weight_preview",
    )
    out = {key: qual.get(key) for key in keys if qual.get(key) is not None}

    promotion_pending = qual.get("promotion_pending")
    if isinstance(promotion_pending, dict):
        out["promotion_pending"] = {
            key: promotion_pending.get(key)
            for key in (
                "pr_number",
                "pr_url",
                "auto_merge_enabled",
                "managed_check_wait",
                "migration_carrying",
                "ci_state",
            )
            if promotion_pending.get(key) is not None
        }

    for source_key, count_key in (
        ("audit_findings", "audit_findings_count"),
        ("excluded_audit_findings", "excluded_audit_findings_count"),
        (
            "challenger_refuted_audit_findings",
            "challenger_refuted_audit_findings_count",
        ),
    ):
        value = qual.get(source_key)
        if isinstance(value, list):
            out[count_key] = len(value)

    for count_key in (
        "audit_findings_total",
        "audit_findings_truncated",
        "excluded_audit_findings_total",
        "excluded_audit_findings_truncated",
    ):
        if qual.get(count_key) is not None:
            out[count_key] = qual.get(count_key)

    return out


def _compose_serving_block(
    aggregate: dict[str, Any],
    artifacts: list[dict[str, Any]],
    version: ReleaseVersion,
) -> dict[str, Any]:
    aggregate_status = aggregate.get("serving_status")
    serving_sha = next(
        (
            e["served_identifier"]
            for e in artifacts
            if e.get("serving_status") == "serving_confirmed"
            and e.get("served_identifier") is not None
        ),
        version.served_sha,
    )
    confirmed_at = next(
        (
            e["served_at"]
            for e in artifacts
            if e.get("serving_status") == "serving_confirmed" and e.get("served_at")
        ),
        _iso(version.served_at),
    )
    return {
        "status": aggregate_status,
        "sha": serving_sha,
        "confirmed_at": confirmed_at,
        "failure_reason": aggregate.get("reason_code")
        if aggregate_status == "deploy_failed"
        else None,
        "artifacts": artifacts,
    }


def _compact_version_lifecycle(
    version: ReleaseVersion,
    project: Project,
    *,
    latest_verdict: "QualificationVerdict | None" = None,
    resolved_qualification: "tuple[dict[str, Any] | None, dict[str, Any] | None] | None" = None,
    qualification_read: Any = None,
) -> dict[str, Any]:
    from app.qualification_state_resolver import resolve_effective_qualification_state
    from app.release_artifacts import version_serving_summary

    aggregate_serving, artifacts = version_serving_summary(version, project)
    qual = (
        version.last_qualification
        if isinstance(version.last_qualification, dict)
        else {}
    )

    # Derive the effective qualification_state through the shared resolver so the
    # finalized-verdict substitution (transient promotion_pending superseded by a
    # finalized verdict row) is applied identically across every read surface.
    # For the OPEN version the caller pre-resolves the state through the async
    # ``resolve_open_version_qualification_state`` seam (which ALSO applies the
    # version-scoped audit-crash reconciliation the sync path here cannot reach
    # without a DB query) and threads it in via ``resolved_qualification`` so the
    # open version agrees with the strategic payload / admin / system-state
    # surfaces. Closed rows keep the cheap sync path (verdict substitution only).
    if qualification_read is not None:
        qualification_state = qualification_read.qualification_state
        verdict_disagreement = qualification_read.disagreement
    elif resolved_qualification is not None:
        qualification_state, verdict_disagreement = resolved_qualification
    else:
        qualification_state, verdict_disagreement = resolve_effective_qualification_state(
            version,
            project,
            latest_verdict=latest_verdict,
            serving=(aggregate_serving, artifacts),
        )

    out = {
        "id": str(version.id),
        "version_number": version.version_number,
        "status": version.status,
        "phase": version.phase,
        "goal": version.goal,
        "opened_at": _iso(version.opened_at),
        "phase_changed_at": _iso(version.phase_changed_at),
        "phase_reasoning": version.phase_reasoning,
        "promoted_at": _iso(version.promoted_at),
        "promoted_sha": version.promoted_sha,
        "goal_updated_at": _iso(version.goal_updated_at),
        "qualification_attempts": version.qualification_attempts,
        "qualification_retry_episode": getattr(
            version, "qualification_retry_episode", 0
        ),
        "active_qualification_attempt_id": (
            str(version.active_qualification_attempt_id)
            if getattr(version, "active_qualification_attempt_id", None)
            else None
        ),
        "active_candidate_sha": qual.get("candidate_sha"),
        "last_qualification": _compact_last_qualification(qual),
        "qualification_state": qualification_state,
        "serving_status": version.serving_status,
        "served_sha": version.served_sha,
        "served_at": _iso(version.served_at),
        "deploy_failure_reason": version.deploy_failure_reason,
        "deploy_confirm_deadline": _iso(version.deploy_confirm_deadline),
        "aggregate_serving_status": aggregate_serving["serving_status"],
        "aggregate_deploy_failure_reason": aggregate_serving["reason_code"],
        "artifacts": artifacts,
        "serving": _compose_serving_block(aggregate_serving, artifacts, version),
    }
    if qualification_read is not None:
        out["qualification_provenance"] = qualification_read.provenance
        out["dimension_states"] = qualification_read.dimension_states
    if verdict_disagreement is not None:
        out["qualification_state_disagreement"] = verdict_disagreement
    return out


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


@router.get("/projects/{project_id}/directions")
@limiter.limit("60/minute")
async def agent_fetch_directions_list(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return compact direction listing (id, status, text, priority) for all
    directions in the project. Used as a recovery path when directions_summary
    was dropped entirely under budget pressure so no ids were shipped — the
    agent can call this with no id to discover direction ids, then pull full
    detail with ``./fetch direction <id>``."""
    project = await _resolve_agent_project(project_id, token, session)
    _COMPACT_KEYS = ("id", "status", "text", "priority")
    directions = [
        {k: d[k] for k in _COMPACT_KEYS if k in d}
        for d in (project.directions or [])
        if isinstance(d, dict)
    ]
    return {"status": "success", "data": directions}


@router.get("/projects/{project_id}/messages/open")
@limiter.limit("60/minute")
async def agent_fetch_open_messages_list(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return compact open-message listing (id, ref, type, from_type, content
    excerpt, status). Used as a recovery path when open_messages was dropped
    entirely under budget pressure so no message refs were shipped — the agent
    can call this with no id to see what messages exist, then pull a full
    message with ``./fetch message <ref>``.

    Path is the ``/messages/open`` sub-collection, NOT bare ``/messages`` — the
    latter is owned by the strategic agent's cursor-poll list in
    ``agent_strategic_messages.py`` (``{messages, server_time}``). Keeping this on
    a distinct literal path avoids the route collision where this listing (being
    included first in ``app.main``) silently shadowed that poll. Registered before
    the ``/messages/{ref}`` resolver below so the literal ``open`` segment wins."""
    await _resolve_agent_project(project_id, token, session)
    rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.status == "open",
                )
                .order_by(ProjectMessage.created_at.asc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    _CONTENT_CAP = 200
    items = [
        {
            "id": str(m.id),
            "ref": m.id.hex[:8],
            "type": getattr(m.message_type, "value", str(m.message_type)),
            "from_type": m.from_type,
            "content": (m.content or "")[:_CONTENT_CAP],
            "status": m.status,
        }
        for m in rows
    ]
    return {"status": "success", "data": items}


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
        retired = next(
            (
                r
                for r in ((project.vision or {}).get("_recently_retired_directions") or [])
                if r.get("id") == direction_id
            ),
            None,
        )
        if retired is not None:
            raise HTTPException(
                status_code=410,
                detail={
                    "status": "pruned",
                    "retired_at": retired.get("retired_at"),
                    "reason": retired.get("reason"),
                    "gap_id": retired.get("gap_id"),
                },
            )
        raise HTTPException(status_code=404, detail="Direction not found")
    out = dict(direction)
    # Compute merge_credit summary from the full history before capping.
    full_history = direction.get("research_history") or []
    agg = direction.get("history_aggregate") or {}
    _total_prs_merged = sum(h.get("prs_merged", 0) for h in full_history) + (
        agg.get("total_prs_merged") or 0
    )
    _total_verified = sum(verified_merged_count(h) for h in full_history) + (
        agg.get("total_verified_prs_merged", agg.get("total_prs_merged", 0)) or 0
    )
    _total_proven = sum(proven_merged_count(h) for h in full_history) + (
        agg.get("total_acceptance_proven_merged", 0) or 0
    )
    _total_consumed = sum(
        h.get("acceptance_consumed_merged", 0) for h in full_history
    ) + (agg.get("total_acceptance_consumed_merged", 0) or 0)
    _reachable_counts = [reachable_merged_count(h) for h in full_history]
    _any_reachable_recorded = any(c is not None for c in _reachable_counts) or bool(
        agg.get("total_reachable_prs_merged")
    ) or (agg.get("total_reachable_recorded_cycles", 0) or 0) > 0
    _total_reachable: int | None = None
    if _any_reachable_recorded:
        _total_reachable = sum(
            c for c in _reachable_counts if c is not None
        ) + (agg.get("total_reachable_prs_merged", 0) or 0)
    out["merge_credit"] = {
        "prs_merged": _total_prs_merged,
        "verified_prs_merged": _total_verified,
        "badge_only_prs_merged": max(0, _total_prs_merged - _total_verified),
        "proven_prs_merged": _total_proven,
        "consumed_prs_merged": _total_consumed,
        **({"reachable_prs_merged": _total_reachable} if _total_reachable is not None else {}),
    }
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
    return {
        "status": "success",
        "data": agent_fetch_message_dict(
            msg,
            convergence_link=get_message_link(msg),
        ),
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
    sub_scores, findings, excluded_findings, challenger_refuted_findings, and
    serving_sha (the git SHA of the deployed code producing this response).
    """
    from app.deployed_sha import get_serving_sha  # noqa: PLC0415
    from app.release_qualifier import (  # noqa: PLC0415
        get_latest_qualification_audit_findings,
    )

    await _resolve_agent_project(project_id, token, session)
    serving_sha = get_serving_sha()

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
        return {
            "source": "no_open_version",
            "serving_sha": serving_sha,
            "qualification_trigger_state": {
                "trigger_state": "blocked",
                "open_version_id": None,
                "reason": "no_open_version",
                "summary": "no open release version — nothing to qualify",
            },
        }

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
        "audit_findings_truncated": None,
        "requalification_readiness_reason": None,
        "gating_total": None,
        "gating_resolved": None,
        "promotion_ci_green_gate": None,
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
            # Truncation and progress context so the strategist can understand
            # why re-qualification is blocked (e.g. truncated + unresolved).
            "audit_findings_truncated": readiness["audit_findings_truncated"],
            "requalification_readiness_reason": readiness["reason"],
            "gating_total": readiness["gating_total"],
            "gating_resolved": readiness["gating_resolved"],
            # Populated by the CI-green-gate block below; keep the key present so
            # the response shape is stable whether or not that compute succeeds.
            "promotion_ci_green_gate": None,
        }
    except Exception:
        logger.exception(
            "[AGENT_FETCH] requalification readiness compute failed for project %s; "
            "serving qualification without the readiness signal",
            project_id,
        )

    # Full-suite CI green gate (ADDITIVE, READ-ONLY). Surfaces whether the
    # integration-branch HEAD has a green ci.yml full-matrix run — the same gate
    # the promotion_failed re-driver consults before re-entering qualification.
    # dispatch=False so this read NEVER triggers a workflow run. Best-effort: a
    # GitHub failure degrades to ``promotion_ci_green_gate: None`` without breaking
    # the read or changing any existing key.
    try:
        from app.release_readiness import (  # noqa: PLC0415
            compute_promotion_ci_green_gate,
        )

        requalification_extra["promotion_ci_green_gate"] = (
            await compute_promotion_ci_green_gate(session, project_id, dispatch=False)
        )
    except Exception:
        logger.exception(
            "[AGENT_FETCH] promotion CI green gate compute failed for project %s; "
            "serving qualification without the gate signal",
            project_id,
        )

    # Goal-coverage drift signal (ADDITIVE). Best-effort: failure degrades to
    # goal_coverage_drift: None without breaking the read.
    goal_coverage_drift: dict | None = None
    try:
        from app.release_readiness import (  # noqa: PLC0415
            compute_goal_coverage_drift_signal,
        )

        goal_coverage_drift = await compute_goal_coverage_drift_signal(
            session, project_id
        )
    except Exception:
        logger.exception(
            "[AGENT_FETCH] goal_coverage_drift compute failed for project %s; "
            "serving qualification without the drift signal",
            project_id,
        )

    # Qualification trigger explain-surface (ADDITIVE). Answers 'why hasn't
    # qualification fired / what will fire it next' — richer than the old
    # initial-trigger-only signal.  Best-effort: a compute failure degrades to
    # ``qualification_trigger_state: None`` without breaking the read.
    qualification_trigger_state: dict | None = None
    try:
        from app import category_verdict_ledger as _cvl  # noqa: PLC0415
        from app.qualification_triggers import explain_qualification_trigger  # noqa: PLC0415
        from app.release_readiness import compute_requalification_readiness  # noqa: PLC0415
        from app.runtime_config_state import (  # noqa: PLC0415
            get_auto_qualification_triggers_mode,
        )
        from app.time_utils import _utcnow  # noqa: PLC0415

        _lq = open_row.last_qualification or {}
        _status = _lq.get("status")
        _is_candidate_fully_met = await _cvl.is_candidate_fully_met(
            session, open_row.id, _lq.get("candidate_sha")
        )
        _readiness = await compute_requalification_readiness(session, project_id)
        qualification_trigger_state = explain_qualification_trigger(
            phase=open_row.phase or "",
            status=_status,
            error_code=_lq.get("error") if _status == "error" else None,
            qualification_attempts=open_row.qualification_attempts or 0,
            error_redrive_count=int(_lq.get("error_redrive_count") or 0),
            is_candidate_fully_met=_is_candidate_fully_met,
            requalification_ready=_readiness.get("requalification_ready"),
            candidate_sha=_lq.get("candidate_sha"),
            automain_sha=None,  # not fetched in the read path (no GitHub I/O)
            automain_ci_state=None,
            blocker=_lq.get("blocker"),
            in_flight=_status == "pending",
            promotion_in_flight=bool(_lq.get("promotion_pending")),
            last_attempt=_lq if _lq else None,
            knob=get_auto_qualification_triggers_mode(),
            evaluated_at=_utcnow().isoformat(),
        )
    except Exception:
        logger.exception(
            "[AGENT_FETCH] qualification_trigger_state compute failed for project "
            "%s; serving qualification without the trigger-state signal",
            project_id,
        )

    from app.qualification_state_resolver import (
        qualification_read_provenance,
        resolve_dimension_states_from_ledger,
        resolve_newest_qualification_verdict,
    )

    selected_verdict = await resolve_newest_qualification_verdict(session, open_row)
    evidence_verdict = (
        selected_verdict
        if selected_verdict is not None and selected_verdict.audit_findings is not None
        else await resolve_newest_qualification_verdict(
            session, open_row, require_audit_findings=True
        )
    )
    verdict = selected_verdict or evidence_verdict
    qualification_provenance = qualification_read_provenance(
        open_row, selected_verdict
    )
    if qualification_trigger_state is not None:
        qualification_trigger_state["qualification_provenance"] = (
            qualification_provenance
        )
    try:
        qualification_dimension_states = await resolve_dimension_states_from_ledger(
            session, open_row
        )
    except Exception:
        logger.exception(
            "[AGENT_FETCH] qualification dimension provenance failed for project %s",
            project_id,
        )
        qualification_dimension_states = []

    if verdict is not None:
        status_verdict = selected_verdict or verdict
        # Identity fields (candidate_sha, status, finalized_at) come from the
        # Version Phase (last_qualification) when available — it is the
        # authoritative current record and may be newer than the verdict row
        # when a subsequent re-qualification overwrote the open-version record
        # without producing a new verdict. Findings still come from the verdict
        # row because its audit_findings column is immutable and survives
        # re-qualification overwrites.
        #
        # Exception: when last_qualification is in a transient "pending" state
        # with a promotion_pending sub-key AND the verdict row already carries a
        # finalized terminal_status, the verdict is canonical — use it so the
        # agent sees the resolved disposition instead of the stale pending.
        lq = open_row.last_qualification or {}
        lq_status = lq.get("status")

        # Detect (and record) the transient pending vs finalized-verdict
        # disagreement through the shared resolver primitive so the agent fetch
        # verb and the version-phase surface classify this window identically.
        _, source_disagreement = apply_verdict_substitution(
            lq,
            status_verdict,
            active_attempt_id=open_row.active_qualification_attempt_id,
        )
        if source_disagreement is not None:
            resolved_status = status_verdict.terminal_status
            resolved_finalized_at = (
                status_verdict.finalized_at.isoformat()
                if status_verdict.finalized_at
                else None
            )
        else:
            resolved_status = lq_status or verdict.terminal_status
            resolved_finalized_at = lq.get("finalized_at") or (
                verdict.finalized_at.isoformat() if verdict.finalized_at else None
            )

        out = {
            "candidate_sha": lq.get("candidate_sha") or verdict.candidate_sha,
            "finalized_at": resolved_finalized_at,
            "status": resolved_status,
            "confidence": verdict.confidence,
            "sub_scores": verdict.sub_scores,
            "findings": get_latest_qualification_audit_findings(
                open_row, evidence_verdict
            ),
            "excluded_findings": (
                evidence_verdict.excluded_audit_findings
                if evidence_verdict is not None
                else []
            )
            or [],
            "challenger_refuted_findings": (
                evidence_verdict.challenger_refuted_audit_findings
                if evidence_verdict is not None
                else []
            )
            or [],
            "goal_alignment": (
                evidence_verdict.goal_alignment
                if evidence_verdict is not None
                else verdict.goal_alignment
            ),
            "error": lq.get("error") or None,
            "detail": lq.get("detail") or None,
            "blocker": lq.get("blocker") or None,
            # Execution-site of the blocker so an agent reading this surface knows
            # whether a fix on automain clears it next qualification (candidate-side)
            # or is inert until this version itself promotes (deployed-side).
            # Prefer the stored value; derive from the blocker for older records.
            "execution_site": lq.get("execution_site")
            or _blocker_execution_site(lq.get("blocker")),
            "qualify_request_blocked": lq.get("qualify_request_blocked") or None,
            "qualify_request_blocked_at": lq.get("qualify_request_blocked_at") or None,
            "source": "qualification_verdict",
            "source_disagreement": source_disagreement,
            "goal_coverage_drift": goal_coverage_drift,
            "qualification_trigger_state": qualification_trigger_state,
            "qualification_provenance": qualification_provenance,
            "dimension_states": qualification_dimension_states,
            "serving_sha": serving_sha,
            **requalification_extra,
        }

        # Surface promotion-failure context: would_promote, promotion_pr_url,
        # promotion_conflict, promotion_error_detail, promotion_failure_count, and
        # promotion_pending sub-record. Mirrors _compact_last_qualification logic.
        if lq.get("would_promote") is not None:
            out["would_promote"] = lq["would_promote"]
        if lq.get("promotion_pr_url") is not None:
            out["promotion_pr_url"] = lq["promotion_pr_url"]
        if lq.get("promotion_conflict") is not None:
            out["promotion_conflict"] = lq["promotion_conflict"]
        if lq.get("promotion_error_detail") is not None:
            out["promotion_error_detail"] = lq["promotion_error_detail"]
        if lq.get("promotion_failure_count") is not None:
            out["promotion_failure_count"] = lq["promotion_failure_count"]

        promotion_pending = lq.get("promotion_pending")
        if isinstance(promotion_pending, dict):
            out["promotion_pending"] = {
                key: promotion_pending.get(key)
                for key in (
                    "pr_number",
                    "pr_url",
                    "auto_merge_enabled",
                    "managed_check_wait",
                    "migration_carrying",
                    "ci_state",
                )
                if promotion_pending.get(key) is not None
            }

        return out

    qual = open_row.last_qualification or {}
    out = {
        "candidate_sha": qual.get("candidate_sha"),
        "finalized_at": qual.get("finalized_at"),
        "status": qual.get("status"),
        "confidence": qual.get("confidence"),
        "sub_scores": qual.get("sub_scores"),
        "findings": get_latest_qualification_audit_findings(open_row),
        "excluded_findings": qual.get("excluded_audit_findings") or [],
        "challenger_refuted_findings": qual.get("challenger_refuted_audit_findings")
        or [],
        "goal_alignment": qual.get("goal_alignment"),
        "error": qual.get("error") or None,
        "detail": qual.get("detail") or None,
        "blocker": qual.get("blocker") or None,
        "qualify_request_blocked": qual.get("qualify_request_blocked") or None,
        "qualify_request_blocked_at": qual.get("qualify_request_blocked_at") or None,
        "source": "last_qualification",
        "goal_coverage_drift": goal_coverage_drift,
        "qualification_trigger_state": qualification_trigger_state,
        "qualification_provenance": qualification_provenance,
        "dimension_states": qualification_dimension_states,
        "serving_sha": serving_sha,
        **requalification_extra,
    }

    # Surface promotion-failure context: would_promote, promotion_pr_url,
    # promotion_conflict, promotion_error_detail, promotion_failure_count, and
    # promotion_pending sub-record. Mirrors _compact_last_qualification logic.
    if qual.get("would_promote") is not None:
        out["would_promote"] = qual["would_promote"]
    if qual.get("promotion_pr_url") is not None:
        out["promotion_pr_url"] = qual["promotion_pr_url"]
    if qual.get("promotion_conflict") is not None:
        out["promotion_conflict"] = qual["promotion_conflict"]
    if qual.get("promotion_error_detail") is not None:
        out["promotion_error_detail"] = qual["promotion_error_detail"]
    if qual.get("promotion_failure_count") is not None:
        out["promotion_failure_count"] = qual["promotion_failure_count"]

    promotion_pending = qual.get("promotion_pending")
    if isinstance(promotion_pending, dict):
        out["promotion_pending"] = {
            key: promotion_pending.get(key)
            for key in (
                "pr_number",
                "pr_url",
                "auto_merge_enabled",
                "managed_check_wait",
                "migration_carrying",
                "ci_state",
            )
            if promotion_pending.get(key) is not None
        }

    return out


@router.get("/projects/{project_id}/version_phase")
@router.get("/projects/{project_id}/version-phase")
@limiter.limit("60/minute")
async def agent_fetch_version_phase(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return compact release-version lifecycle context for strategic recovery.

    Read-only and project-scoped. This intentionally omits full audit findings
    and task changelogs; agents that need the full qualification finding list can
    use ``./fetch qualification`` explicitly.
    """
    project = await _resolve_agent_project(project_id, token, session)

    open_row = (
        (
            await session.execute(
                select(ReleaseVersion)
                .options(selectinload(ReleaseVersion.deploy_artifacts))
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

    recent_closed_rows = (
        (
            await session.execute(
                select(ReleaseVersion)
                .options(selectinload(ReleaseVersion.deploy_artifacts))
                .where(
                    ReleaseVersion.project_id == project_id,
                    ReleaseVersion.status != "open",
                )
                .order_by(ReleaseVersion.opened_at.desc())
                .limit(_RECENT_CLOSED_VERSIONS_KEEP)
            )
        )
        .scalars()
        .all()
    )

    # Goal-coverage drift signal for the open version (ADDITIVE). Best-effort:
    # failure degrades to goal_coverage_drift: None without breaking the read.
    version_phase_drift: dict | None = None
    if open_row is not None:
        try:
            from app.release_readiness import (  # noqa: PLC0415
                compute_goal_coverage_drift_signal,
            )

            version_phase_drift = await compute_goal_coverage_drift_signal(
                session, project_id
            )
        except Exception:
            logger.exception(
                "[AGENT_FETCH] goal_coverage_drift compute failed for project %s",
                project_id,
            )

    # Criterion coverage for the open version (ADDITIVE). Best-effort: failure
    # degrades to criterion_coverage absent from current_version without breaking
    # the read. Gives the agent the canonical per-criterion coverage ledger when
    # the spawn payload was trimmed or dropped under budget pressure.
    version_phase_criterion_coverage: dict | None = None
    if open_row is not None:
        try:
            from app.acceptance_criteria import (  # noqa: PLC0415
                get_version_coverage_summary,
            )

            version_phase_criterion_coverage = await get_version_coverage_summary(
                session, open_row.id
            )
        except Exception:
            logger.exception(
                "[AGENT_FETCH] criterion_coverage compute failed for project %s",
                project_id,
            )

    # Resolve the OPEN version's qualification_state through the shared async
    # seam so it applies ALL THREE reconciliation enrichments (serving aggregate,
    # version-scoped audit child, and the newest finalized verdict) in the same
    # fixed order as the strategic payload, admin lifecycle endpoint, and
    # system-state snapshot. Previously this surface passed only the finalized
    # verdict (no audit-crash reconciliation), so it told a ``qualifying`` story
    # in the audit-crashed window while the operator panel already read
    # ``audit_crashed``. The helper resolves the canonical newest verdict itself.
    _open_resolved_qual: tuple[dict | None, dict | None] | None = None
    _open_qualification_read: Any = None
    if open_row is not None:
        from app.qualification_state_resolver import (  # noqa: PLC0415
            resolve_open_version_qualification_read_model,
        )

        _open_qualification_read = await resolve_open_version_qualification_read_model(
            session, open_row, project
        )
        _open_resolved_qual = (
            _open_qualification_read.qualification_state,
            _open_qualification_read.disagreement,
        )

    _current_version_data = (
        _compact_version_lifecycle(
            open_row,
            project,
            resolved_qualification=_open_resolved_qual,
            qualification_read=_open_qualification_read,
        )
        if open_row is not None
        else None
    )
    if _current_version_data is not None and version_phase_criterion_coverage is not None:
        _current_version_data["criterion_coverage"] = version_phase_criterion_coverage

    # Candidate-currency signal for the fetch/recovery path — same HEAD-vs-candidate
    # comparison the spawn payload provides in payload_staleness.  Best-effort:
    # a GitHub API failure must not break the endpoint. When freshness is
    # indeterminate (currency=None), include a minimal marker with verdict="unknown"
    # so the agent can distinguish indeterminate from absent/no-open-version.
    candidate_currency: dict | None = None
    if open_row is not None:
        try:
            from app.strategic_spawning import (  # noqa: PLC0415
                _build_payload_staleness_block,
                _resolve_candidate_currency,
                get_project_integration_branch,
            )

            _last_qual = open_row.last_qualification or {}
            _integration_branch = get_project_integration_branch(project)
            _raw_currency = await _resolve_candidate_currency(
                session, project, _last_qual, _integration_branch
            )
            # Typed currency block: resolved (fresh/stale) merges the raw
            # current/candidate_sha/head_sha keys with the freshness block for
            # backward compat; api_unreachable and unknown carry their verdict and
            # reason WITHOUT a stale flag so the agent distinguishes a transient
            # GitHub outage from genuine indeterminacy.
            _staleness = _build_payload_staleness_block(_raw_currency)
            if isinstance(_raw_currency, dict) and isinstance(
                _raw_currency.get("current"), bool
            ):
                candidate_currency = {**_raw_currency, **_staleness}
            else:
                candidate_currency = _staleness
        except Exception:
            logger.exception(
                "[AGENT_FETCH] candidate_currency resolve failed for project %s",
                project_id,
            )
            # On exception, also include the indeterminate marker.
            from app.strategic_spawning import _freshness_generated_at  # noqa: PLC0415

            candidate_currency = {
                "verdict": "unknown",
                "generated_at": _freshness_generated_at(),
            }

    return {
        "status": "success",
        "data": {
            "source": "open_version" if open_row is not None else "no_open_version",
            "current_version": _current_version_data,
            "recent_versions": [
                _compact_version_lifecycle(row, project) for row in recent_closed_rows
            ],
            "goal_coverage_drift": version_phase_drift,
            **({"candidate_currency": candidate_currency} if candidate_currency is not None else {}),
        },
    }


@router.get("/projects/{project_id}/vm-cost")
@limiter.limit("60/minute")
async def agent_fetch_vm_cost(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return per-task-type VM cost breakdown (cost, task count, merge count,
    merge-yield) over the last 7 days. Reuses compute_vm_cost_by_task_type from
    efficiency_snapshot so the numbers match the operator dashboard exactly.
    Read-only; cross-project access returns 404."""
    from app.efficiency_snapshot import (  # noqa: PLC0415
        _AXIS_WINDOW_DAYS,
        _load_recent_tasks,
        compute_vm_cost_by_task_type,
    )

    _COHORT_CAP = 5000
    await _resolve_agent_project(project_id, token, session)
    tasks = await _load_recent_tasks(session, project_id)
    by_task_type = compute_vm_cost_by_task_type(tasks)
    cohort_task_count = len(tasks)
    return {
        "status": "success",
        "data": {
            "by_task_type": by_task_type,
            "window_days": _AXIS_WINDOW_DAYS,
            "cohort_task_count": cohort_task_count,
            "truncated": cohort_task_count >= _COHORT_CAP,
        },
    }


@router.get("/projects/{project_id}/efficiency-snapshot")
@limiter.limit("60/minute")
async def agent_fetch_efficiency_snapshot(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return the current efficiency snapshot for the project: cost_per_merged_pr,
    vm_reuse_rate, llm_cost_per_merged_pr, format_compliance, and window metadata.
    Reuses the same compute path as the operator dashboard so numbers are consistent.
    Read-only; cross-project access returns 404."""
    from app.efficiency_snapshot import (  # noqa: PLC0415
        AXIS_COST_PER_MERGED_PR,
        AXIS_COST_PER_VERIFIED_TASK,
        AXIS_LLM_CACHE_HIT_RATE,
        AXIS_LLM_COST_PER_MERGED_PR,
        AXIS_LLM_COST_PER_VERIFIED_TASK,
        AXIS_VM_REUSE_RATE,
        FORMAT_COMPLIANCE_KEY,
        _AXIS_WINDOW_DAYS,
        _compute_cost_per_merged_pr,
        _compute_cost_per_verified_task,
        _compute_llm_cache_hit_rate,
        _compute_vm_reuse_rate,
        _load_recent_tasks,
    )
    from app.llm_cost_attribution import (  # noqa: PLC0415
        _compact_llm_cost_attribution,
        _compute_llm_cost_per_merged_pr,
        _compute_llm_cost_per_verified_task,
    )
    from app.digest_metrics import compute_format_compliance_metrics  # noqa: PLC0415

    await _resolve_agent_project(project_id, token, session)
    tasks = await _load_recent_tasks(session, project_id)
    format_compliance = compute_format_compliance_metrics(tasks)
    llm_cost_attribution = _compact_llm_cost_attribution(tasks)

    return {
        "status": "success",
        "data": {
            AXIS_COST_PER_MERGED_PR: _compute_cost_per_merged_pr(tasks),
            AXIS_COST_PER_VERIFIED_TASK: _compute_cost_per_verified_task(tasks),
            AXIS_VM_REUSE_RATE: _compute_vm_reuse_rate(tasks),
            AXIS_LLM_COST_PER_MERGED_PR: _compute_llm_cost_per_merged_pr(tasks),
            AXIS_LLM_COST_PER_VERIFIED_TASK: _compute_llm_cost_per_verified_task(tasks),
            AXIS_LLM_CACHE_HIT_RATE: _compute_llm_cache_hit_rate(tasks),
            FORMAT_COMPLIANCE_KEY: format_compliance,
            "llm_cost_attribution": llm_cost_attribution,
            "window_days": _AXIS_WINDOW_DAYS,
        },
    }


@router.get("/projects/{project_id}/capability-trials")
@limiter.limit("60/minute")
async def agent_fetch_capability_trials(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return all capability-trial verdict entries for the project.

    Returns the full ``capability_adoption_verdicts`` dict, keyed by cap_name.
    Each entry includes: verdict, verdict_reason, lift, n_with, n_without,
    arm_composition, thresholds, and — when the admin digest has run —
    accrual_projection and is_stalled. Read-only; agent-token auth only.
    Cross-project access returns 404."""
    project = await _resolve_agent_project(project_id, token, session)
    from app.capability_trial_verdict import get_trial_verdicts  # noqa: PLC0415

    verdicts = get_trial_verdicts(project)
    return {
        "status": "success",
        "data": {
            "verdicts": verdicts,
        },
    }


@router.get("/projects/{project_id}/compute-sweeps")
@limiter.limit("60/minute")
async def agent_fetch_compute_sweeps(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return grouped compute-experiment sweep evidence for the project.

    Returns ``{sweep_id: [entry, ...]}`` for sweeps that have at least two
    documents (the same grouping that the strategic payload packs under
    ``compute_experiment_sweeps``). Used for recovery when that field was
    dropped under budget pressure. Read-only; cross-project access returns 404.
    """
    await _resolve_agent_project(project_id, token, session)
    groups = await build_compute_sweep_groups(session, project_id)
    return {"status": "success", "data": groups}


@router.get("/projects/{project_id}/compute-sweeps/{sweep_id}")
@limiter.limit("60/minute")
async def agent_fetch_compute_sweep(
    request: Request,
    project_id: uuid.UUID,
    sweep_id: str,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return all compute-experiment docs for a single sweep, oldest-first.

    Covers both per-arm docs (arm_label set) and the sweep-aggregate doc
    (arm_label null). Used when the strategist needs to recover the full arm
    evidence for one sweep without fetching all groups. Read-only; cross-project
    access returns 404.
    """
    await _resolve_agent_project(project_id, token, session)
    docs = await build_sweep_knowledge_index(session, project_id, sweep_id)
    if not docs:
        raise HTTPException(status_code=404, detail="Sweep not found")
    return {"status": "success", "data": docs}


# ---------------------------------------------------------------------------
# Payload-section fetch inventory + generic section recovery endpoint
# ---------------------------------------------------------------------------

# Maps every _STRATEGIC_REVIEW_SCHEMA key to its recovery path:
#   - a fetch verb string ("problem-model", "direction", ...) — dedicated endpoint
#   - "sections" — served by agent_fetch_payload_section below
#   - None — no recovery (ephemeral, in-memory, or always-critical)
# This dict must stay in sync with _STRATEGIC_REVIEW_SCHEMA; the inventory
# test enforces that every schema key has an explicit entry here.
SECTION_FETCH_INVENTORY: dict[str, str | None] = {
    # Critical identity/routing — always ships, never dropped
    "type": None, "research_mode": None, "project_name": None, "base_branch": None,
    "is_self_repo": None, "enable_self_improvement": None, "model": None, "effort": None,
    "vision": None, "effective_purpose": None, "direction_counts": None,
    "max_active_directions": None,
    "open_message_count": None,
    # High — dedicated fetch verbs
    "open_messages": "message",
    "directions_summary": "direction",
    "problem_model": "problem-model",
    "version_phase": "version-phase",
    # Medium/high — dedicated fetch verbs
    "design_doc": "section",
    "knowledge_index": "knowledge",
    "pending_followups": "knowledge",
    "compute_experiment_sweeps": "compute-sweeps",
    "capability_trial_verdicts": "capability-trials",
    "efficiency_snapshot": "efficiency-snapshot",
    "ci_compliance": "ci-compliance",
    "open_completion_debt": "completion-debt",
    # Low — generic section endpoint (project-data-based, no complex async)
    "self_reflection_log": "sections",
    "strategic_memory_notes": "sections",
    "anomaly_summary": "sections",
    "playbook_summary": "sections",
    "retired_directions_summary": "sections",
    "rejected_proposals": "sections",
    "past_retirement_critiques": "sections",
    "previous_decision_outcomes": "sections",
    "outcome_evaluation": "sections",
    # High/medium exempt — ephemeral, bounded at build time, or tiny scalars
    "prompt_suffix": None, "running_update": None,
    "running_update_acknowledged_at": None, "user_last_active_at": None,
    "first_autonomous_improvement_at": None,
    "operator_context_refs": None, "cycle_start_reconciliation": None,
    "model_health": None, "prune_candidates": None, "playbook_keys_index": None,
    "staged_directions": None, "retirement_deferrals": None,
    "payload_staleness": None, "deployment_state": None,
    "operator_overrides": None, "missing_capability_demand": None,
    "available_capabilities": None, "local_runner_status": None,
    # Tiny autonomous session cadence scalars — runtime config, no fetch path
    "strategic_session_idle_tick_seconds": None,
    "strategic_session_min_tick_seconds": None,
    "strategic_session_max_ops_per_turn": None,
    "strategic_session_message_poll_seconds": None,
    "strategic_session_turn_timeout_seconds": None,
    "strategic_session_autonomous_turn_timeout_seconds": None,
    # Medium — no recovery (require complex async or ephemeral session data)
    "conversation_thread": None,
    "recent_handled_conversation": None,  # exempt; recently-answered operator tail, bounded at build time
    "recent_interventions": None,   # requires GitHub API fan-out
    "drift_signals": None,          # requires async DB compute
    "memo_citations": None,         # requires cycle-scoped Task query
    # Low — no recovery (in-memory runtime state or tiny unactionable scalars)
    "held_directions": None,        # in-memory shared state, not persisted
    "running_update_superseded": None,
    "prediction_calibration_factor": None,
    "prediction_calibration_note": None,
    "system_maturity": None,
    # Low — dedicated fetch verb; tiny persisted trend across recent cycles
    "integrity_conflict_trend": "integrity-conflict-trend",
    # Medium — dedicated fetch verbs; operator-facing tools/skills readable by strategist
    "skills": "skills",
    "project_tools": "tools",
}

_SECTIONS_NEEDING_CYCLE_HISTORY: frozenset[str] = frozenset({
    "past_retirement_critiques",
    "previous_decision_outcomes",
    "outcome_evaluation",
})


@router.get("/projects/{project_id}/sections/{section_name}")
@limiter.limit("60/minute")
async def agent_fetch_payload_section(
    request: Request,
    project_id: uuid.UUID,
    section_name: str,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return a freshly-built payload section by name for sections that have no
    dedicated fetch verb. Only sections registered as 'sections' in
    SECTION_FETCH_INVENTORY are served here; others get 404. Used as the
    recovery path when a low-priority section was dropped under budget pressure.
    Cross-project access returns 404 (not 403)."""
    if SECTION_FETCH_INVENTORY.get(section_name) != "sections":
        raise HTTPException(status_code=404, detail="Section not available via this endpoint")
    project = await _resolve_agent_project(project_id, token, session)

    from datetime import timedelta  # noqa: PLC0415
    from app.strategic_spawning import (  # noqa: PLC0415
        _build_anomaly_summary,
        _build_playbook_summary,
        _build_past_retirement_critiques,
        _build_previous_decision_outcomes,
    )
    from app.strategic_analysis import (  # noqa: PLC0415
        _build_retired_directions_summary,
        _build_outcome_evaluation,
    )
    from app import strategic_cycle as _sc  # noqa: PLC0415
    from app.time_utils import _utcnow  # noqa: PLC0415

    if section_name == "self_reflection_log":
        data: Any = project.self_reflection_log or []
    elif section_name == "strategic_memory_notes":
        data = project.strategic_memory_notes or []
    elif section_name == "anomaly_summary":
        data = _build_anomaly_summary(project)
    elif section_name == "playbook_summary":
        data = _build_playbook_summary(project)
    elif section_name == "retired_directions_summary":
        data = _build_retired_directions_summary(project)
    elif section_name == "rejected_proposals":
        cutoff = (_utcnow() - timedelta(days=30)).isoformat()
        rows = [
            {
                "text": (d.get("text") or "")[:300],
                "rejected_at": d.get("rejected_at"),
                "rationale": (d.get("rationale") or "")[:300] if d.get("rationale") else None,
            }
            for d in (project.directions or [])
            if d.get("status") == "rejected" and (d.get("rejected_at") or "") >= cutoff
        ]
        rows.sort(key=lambda r: r.get("rejected_at") or "", reverse=True)
        data = rows[:10]
    else:
        # past_retirement_critiques, previous_decision_outcomes, outcome_evaluation
        cycle_history = await _sc.recent_for_agent_context(session, project.id)
        if section_name == "past_retirement_critiques":
            data = _build_past_retirement_critiques(cycle_history)
        elif section_name == "previous_decision_outcomes":
            data = _build_previous_decision_outcomes(project, cycle_history)
        else:
            data = _build_outcome_evaluation(project, cycle_history)

    return {"status": "success", "data": data}


@router.get("/projects/{project_id}/integrity-conflict-trend")
@limiter.limit("60/minute")
async def agent_fetch_integrity_conflict_trend(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return the persisted integrity-conflict trend for the project.

    Queries the last 10 completed strategic cycles for their stamped
    integrity-conflict counts (orphaned_gaps, ungrounded_directions,
    stale_active_moves, staffed_but_deferred) and returns the trend summary:
    stamped_cycles, consecutive_clean_cycles, and last_counts.  Used by the
    strategist to verify the '2 consecutive clean cycles' retirement condition
    for the direction-gap referential-integrity direction."""
    project = await _resolve_agent_project(project_id, token, session)
    from app import strategic_cycle as _sc  # noqa: PLC0415

    _ = project  # resolve auth; project_id is passed directly to the query
    trend = await _sc.recent_integrity_conflict_trend(session, project_id, limit=10)
    return {"status": "success", "data": trend}


@router.get("/projects/{project_id}/completion-debt")
@limiter.limit("60/minute")
async def agent_fetch_completion_debt(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return open completion-debt entries for the project.

    Recovery path when ``open_completion_debt`` was dropped from the strategic
    payload under budget pressure. Returns the bounded list of open debt entries
    (kind, description, created_at, age_days) from the completion-debt ledger.

    Returns ``{"status": "success", "data": [], "source": "not_available"}`` when
    the completion-debt module has not yet been deployed (ledger table absent).
    Cross-project access returns 404.
    """
    await _resolve_agent_project(project_id, token, session)
    try:
        from importlib import import_module

        _completion_debt = import_module("app.completion_debt")
        entries = await _completion_debt.get_open_completion_debt(session, project_id)
        return {"status": "success", "data": entries}
    except Exception:
        logger.exception(
            "[AGENT_FETCH] completion_debt unavailable for project %s", project_id
        )
        return {"status": "success", "data": [], "source": "not_available"}


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


@router.get("/projects/{project_id}/spend-ceiling")
@limiter.limit("60/minute")
async def agent_fetch_spend_ceiling(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return per-project spend ceiling context including whether the ceiling
    has been reached.  Returns ``spend_ceiling_usd``, ``spend_recent_usd``, and
    ``spend_ceiling_reached``.  The ceiling is read from the project's resource
    envelope; when no ceiling is configured all three fields are null.

    Read-only; cross-project access returns 404."""
    project = await _resolve_agent_project(project_id, token, session)
    from app.resource_envelope import (  # noqa: PLC0415
        get_project_recent_spend_usd,
        resolve_project_spend_ceiling,
    )

    ceiling = resolve_project_spend_ceiling(project)
    if ceiling is None:
        return {
            "status": "success",
            "data": {
                "spend_ceiling_usd": None,
                "spend_recent_usd": None,
                "spend_ceiling_reached": None,
            },
        }

    spend = await get_project_recent_spend_usd(session, project_id=project_id)
    recent_total = spend["total"]
    return {
        "status": "success",
        "data": {
            "spend_ceiling_usd": ceiling,
            "spend_recent_usd": recent_total,
            "spend_ceiling_reached": recent_total >= ceiling,
        },
    }


@router.get("/projects/{project_id}/ui-load-timing")
@limiter.limit("60/minute")
async def agent_fetch_ui_load_timing(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return admin-UI initial-load timing samples grouped by surface.

    Mirrors GET /api/admin/ui-load-timing so the strategic agent can verify the
    >50% page-ready improvement retirement threshold without a human JWT. Each
    surface entry is a list of {ts, page_ready_ms, sample_type} objects ordered
    oldest-first (up to 5000 rows total). Read-only; cross-project access returns 404."""
    await _resolve_agent_project(project_id, token, session)
    rows = (
        await session.execute(
            select(UiLoadTimingSample)
            .order_by(UiLoadTimingSample.recorded_at.asc())
            .limit(5000)
        )
    ).scalars().all()
    surfaces: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        surfaces.setdefault(row.surface, []).append(
            {"ts": row.recorded_at.isoformat(), "page_ready_ms": row.page_ready_ms, "sample_type": row.sample_type}
        )
    return {"status": "success", "data": {"surfaces": surfaces}}


@router.get("/projects/{project_id}/skills")
@limiter.limit("60/minute")
async def agent_fetch_project_skills(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Fetch skills declared in the project's ``.blueprint/tools.json`` manifest.

    Returns ``{"skills": [...]}`` where each skill entry has ``slug``,
    ``title``, ``command``, and optionally ``description``.  An empty array
    means no skills are declared.  Authenticated via agent JWT; cross-project
    access returns 404.

    This endpoint is the backend for the strategic agent's ``./fetch skills``
    verb.
    """
    project = await _resolve_agent_project(project_id, token, session)
    from app.routers.admin_project_tools import _discover_manifest_tools  # noqa: PLC0415

    manifest = await _discover_manifest_tools(project)
    return {"skills": manifest.get("skills", [])}


@router.get("/projects/{project_id}/tools")
@limiter.limit("60/minute")
async def agent_fetch_project_tools(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Fetch the serialized content of the project's ENABLED built-in tools.

    Returns ``{"tools": [...]}`` where each entry has ``slug``, ``title``,
    ``kind``, and ``text`` — the notes markdown text or a whiteboard
    presence/stroke-count summary (see :func:`load_project_tool_context`).
    Tools with empty content are skipped and the overall payload is size-capped.
    An empty array means no enabled tool has renderable content. Authenticated
    via agent JWT; cross-project access returns 404.

    This endpoint is the backend for the strategic agent's ``./fetch tools``
    verb — the operator-facing companion to the task-claim ``project_tools``
    injection, so the strategist can read the operator's notes/sketches.
    """
    project = await _resolve_agent_project(project_id, token, session)
    from app.project_tool_context import load_project_tool_context  # noqa: PLC0415

    return {"tools": await load_project_tool_context(session, project.id)}
