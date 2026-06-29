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
from app.knowledge_query import build_compute_sweep_groups, build_sweep_knowledge_index
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
_RECENT_CLOSED_VERSIONS_KEEP = 5


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt else None


def _compact_last_qualification(qual: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return lifecycle-safe qualification summary without full audit payloads."""
    if not isinstance(qual, dict) or not qual:
        return None

    keys = (
        "status",
        "candidate_sha",
        "confidence",
        "threshold",
        "started_at",
        "finalized_at",
        "blocker",
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
) -> dict[str, Any]:
    from app import release_version as _release_version
    from app.release_artifacts import version_serving_summary

    aggregate_serving, artifacts = version_serving_summary(version, project)
    qual = (
        version.last_qualification
        if isinstance(version.last_qualification, dict)
        else {}
    )
    return {
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
        "active_candidate_sha": qual.get("candidate_sha"),
        "last_qualification": _compact_last_qualification(qual),
        "qualification_state": _release_version.summarize_qualification_state(
            qual,
            version.serving_status,
            version.deploy_failure_reason,
            aggregate_serving_status=aggregate_serving["serving_status"],
            aggregate_deploy_failure_reason=aggregate_serving["reason_code"],
            artifact_evidence=artifacts,
        ),
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
    type_value = (
        message_type.value if hasattr(message_type, "value") else str(message_type)
    )
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
            "finalized_at": verdict.finalized_at.isoformat()
            if verdict.finalized_at
            else None,
            "status": verdict.terminal_status,
            "confidence": verdict.confidence,
            "sub_scores": verdict.sub_scores,
            "findings": get_latest_qualification_audit_findings(open_row, verdict),
            "excluded_findings": verdict.excluded_audit_findings or [],
            "challenger_refuted_findings": verdict.challenger_refuted_audit_findings
            or [],
            "goal_alignment": verdict.goal_alignment,
            "source": "qualification_verdict",
            "goal_coverage_drift": goal_coverage_drift,
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
        "challenger_refuted_findings": qual.get("challenger_refuted_audit_findings")
        or [],
        "goal_alignment": qual.get("goal_alignment"),
        "source": "last_qualification",
        "goal_coverage_drift": goal_coverage_drift,
        **requalification_extra,
    }


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

    _current_version_data = (
        _compact_version_lifecycle(open_row, project) if open_row is not None else None
    )
    if _current_version_data is not None and version_phase_criterion_coverage is not None:
        _current_version_data["criterion_coverage"] = version_phase_criterion_coverage

    return {
        "status": "success",
        "data": {
            "source": "open_version" if open_row is not None else "no_open_version",
            "current_version": _current_version_data,
            "recent_versions": [
                _compact_version_lifecycle(row, project) for row in recent_closed_rows
            ],
            "goal_coverage_drift": version_phase_drift,
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

    await _resolve_agent_project(project_id, token, session)
    tasks = await _load_recent_tasks(session, project_id)
    by_task_type = compute_vm_cost_by_task_type(tasks)
    return {
        "status": "success",
        "data": {
            "by_task_type": by_task_type,
            "window_days": _AXIS_WINDOW_DAYS,
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
        AXIS_LLM_COST_PER_MERGED_PR,
        AXIS_VM_REUSE_RATE,
        FORMAT_COMPLIANCE_KEY,
        _AXIS_WINDOW_DAYS,
        _compute_cost_per_merged_pr,
        _compute_vm_reuse_rate,
        _load_recent_tasks,
        _resolve_deploy_since,
        compute_gate_health_verdict,
        compute_panel_review_effectiveness,
    )
    from app.llm_cost_attribution import (  # noqa: PLC0415
        _compact_llm_cost_attribution,
        _compute_llm_cost_per_merged_pr,
    )
    from app.digest_metrics import compute_format_compliance_metrics  # noqa: PLC0415

    await _resolve_agent_project(project_id, token, session)
    tasks = await _load_recent_tasks(session, project_id)
    format_compliance = compute_format_compliance_metrics(tasks)
    llm_cost_attribution = _compact_llm_cost_attribution(tasks)

    # Rolling-window panel-review gate effectiveness plus its calibrated fail-open
    # health verdict, so the strategist sees the same HEALTHY/PARTIAL/DEGRADED/
    # INSUFFICIENT/NO_DATA classification the operator dashboard shows.
    panel_review_effectiveness = compute_panel_review_effectiveness(tasks)
    gate_health = compute_gate_health_verdict(panel_review_effectiveness)

    # Deploy-scoped companion: restrict the same metric to tasks completed after
    # the post-deployment boundary (earliest snapshot under the current serving
    # SHA, derived identically to compute_current_axes). None when no boundary is
    # known — never fabricated — so the strategist can confirm the per-deployment
    # ≤5% fail-open target specifically.
    deploy_since = await _resolve_deploy_since(session, project_id)
    panel_review_effectiveness_post_deploy = (
        compute_panel_review_effectiveness(tasks, since_dt=deploy_since)
        if deploy_since is not None
        else None
    )
    gate_health_post_deploy = (
        compute_gate_health_verdict(panel_review_effectiveness_post_deploy)
        if deploy_since is not None
        else None
    )

    return {
        "status": "success",
        "data": {
            AXIS_COST_PER_MERGED_PR: _compute_cost_per_merged_pr(tasks),
            AXIS_VM_REUSE_RATE: _compute_vm_reuse_rate(tasks),
            AXIS_LLM_COST_PER_MERGED_PR: _compute_llm_cost_per_merged_pr(tasks),
            FORMAT_COMPLIANCE_KEY: format_compliance,
            "llm_cost_attribution": llm_cost_attribution,
            "panel_review_effectiveness": panel_review_effectiveness,
            "gate_health": gate_health,
            "panel_review_effectiveness_post_deploy": panel_review_effectiveness_post_deploy,
            "gate_health_post_deploy": gate_health_post_deploy,
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
