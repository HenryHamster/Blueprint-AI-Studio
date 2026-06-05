"""Release version lifecycle endpoints."""

import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel, Field as _Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.database import get_session
from app.models import (
    ReleaseVersion,
    StrategicCycle,
    QualificationVerdict,
)
from app.auth import get_current_user
from app.admin import _get_user_project_with_role
from app.shared_state import runtime_config as _runtime_config
from app import release_version as _release_version

router = APIRouter()


_MAX_RETIREMENT_CRITIQUES = 100


@router.get("/projects/{project_id}/retirement-critiques")
async def get_retirement_critiques(
    project_id: uuid.UUID,
    limit: int = Query(default=25, ge=1, le=_MAX_RETIREMENT_CRITIQUES),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return recent retirement critiques stored across StrategicCycle rows.

    Each StrategicCycle may carry a list of retirement critiques written by the
    strategic agent when it retires directions. Returns the most recent `limit`
    critiques across the last 20 completed cycles, ordered newest-cycle-first.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )

    result = await session.execute(
        select(StrategicCycle)
        .options(
            load_only(
                StrategicCycle.id,
                StrategicCycle.cycle_metadata,
                StrategicCycle.created_at,
            )
        )
        .where(StrategicCycle.project_id == project_id)
        .order_by(StrategicCycle.created_at.desc())
        .limit(20)
    )
    cycles = result.scalars().all()

    critiques_out: list[dict] = []
    for cycle in cycles:
        if len(critiques_out) >= limit:
            break
        raw = cycle.cycle_metadata or {}
        critiques = raw.get("retirement_critiques", [])
        if not isinstance(critiques, list):
            continue
        for c in critiques:
            if not isinstance(c, dict):
                continue
            if c.get("no_eligible_retirements"):
                continue
            critiques_out.append(
                {
                    "direction_id": c.get("direction_id"),
                    "what_it_addressed": c.get("what_it_addressed", ""),
                    "pattern_that_produced_it": c.get("pattern_that_produced_it", ""),
                    "lesson": c.get("lesson", ""),
                    "cycle_id": cycle.id,
                    "cycle_created_at": cycle.created_at.isoformat()
                    if cycle.created_at
                    else None,
                }
            )
            if len(critiques_out) >= limit:
                break

    return {"critiques": critiques_out, "total": len(critiques_out)}


def _format_qualification(qual: Dict[str, Any]) -> Dict[str, Any]:
    """Extract operator-facing fields from a last_qualification record."""
    return {
        "status": qual.get("status"),
        "confidence": qual.get("confidence"),
        "threshold": qual.get("threshold"),
        "sub_scores": qual.get("sub_scores"),
        "ci_resolution": qual.get("ci_resolution"),
        "blocker": qual.get("blocker"),
        "finalized_at": qual.get("finalized_at"),
        # Infra-failure context (populated only when status=infra_failure).
        "error": qual.get("error"),
        "consecutive_infra_errors": qual.get("consecutive_infra_errors"),
        # Audit observability: why the audit sub-score is missing/unscoreable.
        "audit_missing_reason": qual.get("audit_missing_reason"),
        # Audit reconciliation: model-reported vs findings-derived score delta
        # and excluded findings count (populated by _compute_audit_reconciliation).
        "audit_reconciliation": qual.get("audit_reconciliation"),
    }


@router.get("/projects/{project_id}/version-lifecycle")
async def get_version_lifecycle(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return the release version lifecycle state for a project.

    Exposes the current open version (phase, qualification verdicts, merged-task
    count) and a short history of recent closed versions for calibration.
    Projects with no version lifecycle return ``null`` for ``current_version``
    and an empty ``recent_versions`` list.
    """
    from app.release_version import count_merged_since  # noqa: PLC0415
    from app.release_qualifier import (  # noqa: PLC0415
        compute_promotion_readiness,
        get_latest_release_audit_report,
    )
    from app.models import Project  # noqa: PLC0415
    from app.strategic_spawning import (  # noqa: PLC0415
        _build_staged_directions_summary,
    )

    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="viewer"
    )

    project_row = (
        await session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()

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

    current_version_out: Optional[Dict[str, Any]] = None
    if open_row is not None:
        merged_count = await count_merged_since(session, project_id, open_row.opened_at)
        qual = open_row.last_qualification or {}
        # Surface the latest release_audit's actual findings/score so a verdict
        # is explainable (the verdict record only keeps the composite, and the
        # task API strips artifact logs — otherwise operators are blind to WHY).
        latest_audit = await get_latest_release_audit_report(session, project_id)

        # Fetch live current_focus from the project's problem_model
        live_current_focus = None
        if project_row and project_row.problem_model:
            intent = project_row.problem_model.get("intent", {})
            live_current_focus = intent.get("current_focus")

        current_version_out = {
            "version_number": open_row.version_number,
            "phase": open_row.phase,
            "phase_changed_at": open_row.phase_changed_at.isoformat()
            if open_row.phase_changed_at
            else None,
            "phase_reasoning": open_row.phase_reasoning,
            "goal": open_row.goal,
            "goal_updated_at": open_row.goal_updated_at.isoformat()
            if open_row.goal_updated_at
            else None,
            "goal_editor_info": open_row.goal_editor_info,
            "live_current_focus": live_current_focus,
            "opened_at": open_row.opened_at.isoformat(),
            "qualification_attempts": open_row.qualification_attempts,
            "merged_task_count": merged_count,
            "last_qualification": _format_qualification(qual) if qual else None,
            "latest_audit": latest_audit,
        }

    history_rows = (
        (
            await session.execute(
                select(ReleaseVersion)
                .where(
                    ReleaseVersion.project_id == project_id,
                    ReleaseVersion.status != "open",
                )
                .order_by(ReleaseVersion.opened_at.desc())
                .limit(5)
            )
        )
        .scalars()
        .all()
    )

    promotion_readiness = await compute_promotion_readiness(session, project_id)

    return {
        "current_version": current_version_out,
        "recent_versions": [
            {
                "version_number": v.version_number,
                "status": v.status,
                "opened_at": v.opened_at.isoformat(),
                "promoted_at": v.promoted_at.isoformat() if v.promoted_at else None,
                "qualification_attempts": v.qualification_attempts,
                "last_qualification": _format_qualification(v.last_qualification)
                if v.last_qualification
                else None,
            }
            for v in history_rows
        ],
        "promotion_enabled": bool(_runtime_config.get("promotion_enabled", False)),
        "promotion_readiness": promotion_readiness,
        # Directions deliberately queued (status=staged) to begin only after the
        # current version promotes. Surfaced read-only so an operator can confirm
        # staged work is queued and spot items rotting past their target version.
        "staged_directions": (
            _build_staged_directions_summary(project_row) if project_row else []
        ),
    }


class _SetVersionPhaseBody(BaseModel):
    phase: str = _Field(..., pattern=r"^(build|stabilize)$")
    reasoning: str = _Field(default="", max_length=500)


@router.post("/projects/{project_id}/version-phase")
async def set_version_phase(
    project_id: uuid.UUID,
    payload: _SetVersionPhaseBody,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Set the phase of the open release version for a project.

    Member+ role required.  Returns 404 when there is no open version.
    Combined with POST /qualify this gives the operator a direct lever to
    advance a version to stabilize and trigger the release gate end-to-end,
    producing a dry-run QualificationVerdict without waiting for the strategic
    agent's conservative stall threshold (~14 days).
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    open_ver = await _release_version.current_open_version(session, project_id)
    if open_ver is None:
        raise HTTPException(
            status_code=404,
            detail="No open release version found for this project.",
        )

    operator_id = current_user.get("email") or current_user["user_id"]
    full_reasoning = (
        f"[operator:{operator_id}] {payload.reasoning}".strip()
        if payload.reasoning
        else f"[operator:{operator_id}]"
    )
    _release_version.set_version_phase(
        open_ver, payload.phase, reasoning=full_reasoning
    )
    session.add(open_ver)
    await session.commit()
    await session.refresh(open_ver)
    return {
        "version_number": open_ver.version_number,
        "phase": open_ver.phase,
        "phase_changed_at": open_ver.phase_changed_at.isoformat()
        if open_ver.phase_changed_at
        else None,
        "phase_reasoning": open_ver.phase_reasoning,
    }


class _SetVersionGoalBody(BaseModel):
    goal: str | None = _Field(default=None, max_length=500)


@router.post("/projects/{project_id}/version-goal")
async def set_version_goal(
    project_id: uuid.UUID,
    payload: _SetVersionGoalBody,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Set the goal of the open release version for a project.

    Member+ role required.  Returns 404 when there is no open version.
    Allows operators to set or refine the version's objective, providing
    intentional control over the version's stated intent.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    open_ver = await _release_version.current_open_version(session, project_id)
    if open_ver is None:
        raise HTTPException(
            status_code=404,
            detail="No open release version found for this project.",
        )

    operator_id = current_user.get("email") or current_user["user_id"]
    _release_version.set_version_goal(
        open_ver, payload.goal, editor_id=operator_id
    )
    session.add(open_ver)
    await session.commit()
    await session.refresh(open_ver)
    return {
        "version_number": open_ver.version_number,
        "goal": open_ver.goal,
        "goal_updated_at": open_ver.goal_updated_at.isoformat()
        if open_ver.goal_updated_at
        else None,
        "goal_editor_info": open_ver.goal_editor_info,
    }


class _AbandonVersionBody(BaseModel):
    reason: str | None = _Field(default=None, max_length=500)


@router.post("/projects/{project_id}/version-cut")
async def cut_version(
    project_id: uuid.UUID,
    payload: _AbandonVersionBody,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Operator-initiated version cut: abandon the open version, open the next.

    Member+ role required.  Returns 404 when there is no open version to cut.
    Abandons the current open ``ReleaseVersion`` (status ``abandoned``, recording
    the operator reason + identity in ``last_qualification`` and
    ``phase_reasoning``) and opens a fresh next-numbered version in the ``build``
    phase with a current-focus goal snapshot.  Gives operators a lever to resolve
    a genuinely-stuck version (wrong goal, pivoted work, un-passable candidate)
    that the existing stall detection only alerts on.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    operator_id = current_user.get("email") or current_user["user_id"]
    abandoned, new_version = await _release_version.abandon_current_version(
        session,
        project_id,
        reason=payload.reason,
        operator_id=operator_id,
    )
    if abandoned is None or new_version is None:
        raise HTTPException(
            status_code=404,
            detail="No open release version found for this project.",
        )

    await session.commit()
    await session.refresh(abandoned)
    await session.refresh(new_version)
    return {
        "abandoned_version": {
            "version_number": abandoned.version_number,
            "status": abandoned.status,
            "abandoned_at": (abandoned.last_qualification or {}).get("abandoned_at"),
        },
        "new_version": {
            "version_number": new_version.version_number,
            "phase": new_version.phase,
            "goal": new_version.goal,
            "opened_at": new_version.opened_at.isoformat()
            if new_version.opened_at
            else None,
        },
    }


@router.get("/projects/{project_id}/audit-findings")
async def get_audit_findings(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return the latest qualification's audit findings for a project.

    Read-only; viewer+ role required. Surfaces the ``audit_findings`` list
    carried into ``last_qualification`` by ``_finalize_qualification`` so
    operators can see which specific issues drove a low audit sub-score.
    Returns an empty list when no open version exists or no qualification
    has run yet.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="viewer"
    )

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
        return {"findings": [], "source": "no_open_version"}

    qual = open_row.last_qualification or {}
    findings = qual.get("audit_findings") or []
    if not isinstance(findings, list):
        findings = []
    return {
        "findings": findings,
        "audit_missing_reason": qual.get("audit_missing_reason"),
        "finalized_at": qual.get("finalized_at"),
        "source": "last_qualification",
    }


_VERDICT_HISTORY_LIMIT = 50


@router.get("/projects/{project_id}/qualification-verdicts")
async def list_qualification_verdicts(
    project_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=_VERDICT_HISTORY_LIMIT),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return recent QualificationVerdict rows for a project, newest-first.

    Viewer+ role required.  Returns a bounded list (max 50) of per-verdict
    history rows covering both dry-run and promotion-enabled regimes, giving
    operators a live view of qualifier behavior across the full history.

    ``infra_context`` accompanies the verdict list so callers can distinguish
    'no recent verdicts because qualifier never ran' from 'qualifier has been
    thrashing on infrastructure failures'.
    """
    from app.release_qualifier import compute_promotion_readiness  # noqa: PLC0415

    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )

    stmt = (
        select(QualificationVerdict)
        .where(QualificationVerdict.project_id == project_id)
        .order_by(QualificationVerdict.created_at.desc())
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars().all())

    readiness = await compute_promotion_readiness(session, project_id)

    return {
        "verdicts": [
            {
                "id": str(v.id),
                "release_version_id": str(v.release_version_id),
                "candidate_sha": v.candidate_sha,
                "confidence": v.confidence,
                "sub_scores": v.sub_scores,
                "blocker": v.blocker,
                "audit_missing_reason": v.audit_missing_reason,
                "would_promote": v.would_promote,
                "promotion_enabled": v.promotion_enabled,
                "terminal_status": v.terminal_status,
                "threshold": v.threshold,
                "started_at": v.started_at.isoformat() if v.started_at else None,
                "finalized_at": v.finalized_at.isoformat() if v.finalized_at else None,
                "created_at": v.created_at.isoformat(),
            }
            for v in rows
        ],
        "total": len(rows),
        "infra_context": {
            "infra_failure_count": readiness.get("infra_failure_count", 0),
            "consecutive_infra_errors": readiness.get("consecutive_infra_errors", 0),
        },
    }
