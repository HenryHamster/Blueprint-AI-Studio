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
from app.auth import get_current_user, require_admin
from app.admin import _get_user_project_with_role
from app.shared_state import (
    promotion_enabled_by_project_map,
    resolve_project_promotion_enabled,
    runtime_config as _runtime_config,
)
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
        # The candidate commit this qualification was evaluated against. Lets the
        # operator UI detect when the latest release_audit's findings were computed
        # against a different (older) commit than the version's evaluated candidate.
        "candidate_sha": qual.get("candidate_sha"),
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
    from app.release_version import (  # noqa: PLC0415
        count_merged_since,
        count_in_flight_tasks,
    )
    from app.release_qualifier import (  # noqa: PLC0415
        compute_promotion_readiness,
        resolve_lifecycle_audit_evidence,
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
        in_flight_count = await count_in_flight_tasks(session, project_id)
        qual = open_row.last_qualification or {}
        # Verdict-staleness signal: how many tasks merged AT/AFTER the open
        # version's last qualification verdict was finalized. Each verdict is
        # evaluated against one candidate commit; merges landing after
        # ``finalized_at`` mean the displayed confidence/sub-scores no longer
        # reflect the current tree. Computed from the verdict's ``finalized_at``
        # (NOT the version's ``opened_at``). ``None`` when there is no verdict or
        # no parseable ``finalized_at`` — never fabricate a count.
        merged_since_verdict: Optional[int] = None
        finalized_at_raw = qual.get("finalized_at")
        if finalized_at_raw:
            try:
                from datetime import datetime  # noqa: PLC0415

                finalized_at_dt = datetime.fromisoformat(finalized_at_raw)
            except (ValueError, TypeError):
                finalized_at_dt = None
            if finalized_at_dt is not None:
                merged_since_verdict = await count_merged_since(
                    session, project_id, finalized_at_dt
                )
        qual_state = _release_version.summarize_qualification_state(
            qual, open_row.serving_status, open_row.deploy_failure_reason
        )
        # Surface the open version's audit findings/score so a verdict is
        # explainable (the verdict record only keeps the composite, and the
        # task API strips artifact logs — otherwise operators are blind to WHY).
        # Scoped to THIS version's evaluated candidate so a fresh/newly-cut
        # version never inherits another version's findings: ``latest_audit`` is
        # populated only when the newest scoped audit matches the evaluated
        # candidate SHA; otherwise the audit is surfaced as ``stale_audit``
        # provenance and ``audit_evidence_state`` explains which case applies.
        audit_evidence = await resolve_lifecycle_audit_evidence(session, open_row)
        latest_audit = audit_evidence["latest_audit"]

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
            # Tasks merged AT/AFTER the last qualification verdict's finalized_at
            # (verdict-staleness signal). None when no verdict/finalized_at.
            "merged_since_verdict": merged_since_verdict,
            # In-flight (non-terminal) tasks that a version cut will NOT stop —
            # they keep running and land against the freshly-opened next version.
            # Surfaced so the cut confirmation can disclose this consequence.
            "in_flight_task_count": in_flight_count,
            "qualification_state": qual_state,
            "last_qualification": _format_qualification(qual) if qual else None,
            "latest_audit": latest_audit,
            # Provenance for version-scoped audit evidence (additive). When the
            # newest scoped audit was run against an OLDER candidate than the one
            # under evaluation, its report is surfaced here (not as latest_audit)
            # so the UI can mark the findings historical. ``audit_evidence_state``
            # is ``current`` | ``stale`` | ``none``.
            "audit_evidence_state": audit_evidence["state"],
            "stale_audit": audit_evidence["stale_audit"],
            # Post-promotion serving confirmation (additive; NULL until the open
            # version is itself promoted — normally NULL for a still-open one).
            "serving_status": open_row.serving_status,
            "served_sha": open_row.served_sha,
            "served_at": open_row.served_at.isoformat()
            if open_row.served_at
            else None,
            "deploy_failure_reason": open_row.deploy_failure_reason,
            "deploy_confirm_deadline": open_row.deploy_confirm_deadline.isoformat()
            if open_row.deploy_confirm_deadline
            else None,
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
                # Post-promotion serving confirmation — disambiguates a merged
                # promotion from one that actually shipped (additive fields).
                "serving_status": v.serving_status,
                "served_sha": v.served_sha,
                "served_at": v.served_at.isoformat() if v.served_at else None,
                "deploy_failure_reason": v.deploy_failure_reason,
                "deploy_confirm_deadline": v.deploy_confirm_deadline.isoformat()
                if v.deploy_confirm_deadline
                else None,
                "qualification_state": _release_version.summarize_qualification_state(
                    v.last_qualification, v.serving_status, v.deploy_failure_reason
                ),
                "abandoned_at": (
                    (v.last_qualification or {}).get("abandoned_at")
                    if v.status == "abandoned"
                    else None
                ),
                "abandoned_by": (
                    (v.last_qualification or {}).get("operator")
                    if v.status == "abandoned"
                    else None
                ),
                "abandon_reason": (
                    (v.last_qualification or {}).get("reason")
                    if v.status == "abandoned"
                    else None
                ),
            }
            for v in history_rows
        ],
        "promotion_enabled": bool(_runtime_config.get("promotion_enabled", False)),
        # Per-project live-promotion enablement. ``promotion_enabled_effective``
        # is the value the promotion gate actually consults for THIS project (the
        # override when set, else the fleet default). ``promotion_enabled_override``
        # is the explicit per-project value (None when the project rides the fleet
        # default), and ``promotion_enabled_source`` tells the operator which one
        # is in effect.
        "promotion_enabled_effective": resolve_project_promotion_enabled(project_id),
        "promotion_enabled_override": promotion_enabled_by_project_map().get(
            str(project_id)
        ),
        "promotion_enabled_source": (
            "project"
            if str(project_id) in promotion_enabled_by_project_map()
            else "fleet"
        ),
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

    # Acquire the open version under FOR UPDATE lock, then re-check preconditions
    # against the freshly-locked row. Between the initial read and this lock,
    # a concurrent promotion/abandon could have changed the version's state.
    open_ver = (
        (
            await session.execute(
                select(ReleaseVersion)
                .where(
                    ReleaseVersion.project_id == project_id,
                    ReleaseVersion.status == "open",
                )
                .order_by(ReleaseVersion.opened_at.desc())
                .limit(1)
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if open_ver is None:
        raise HTTPException(
            status_code=404,
            detail="No open release version found for this project.",
        )

    # Refuse a phase flip while a promotion is in flight (audit passed, promotion
    # PR open and auto-merging).  Flipping back to build here clears the
    # "promotion in flight" indication in the panel even though nothing was
    # cancelled — leaving the operator levers disagreeing about the version's
    # real state.  Mirrors the version-cut lever's 409 refusal on the same
    # predicate. (Only the operator endpoint guards this — the internal
    # set_version_phase helper stays open for finalize/sweep/promotion flows that
    # legitimately flip phase mid-promotion.)
    # Re-check this against the LOCKED row's last_qualification, not a stale read.
    _qual_state = _release_version.summarize_qualification_state(
        open_ver.last_qualification
    )
    if _qual_state and _qual_state.get("state") == "promotion_in_flight":
        detail = (
            "a promotion PR is open and auto-merging — wait for it to land or "
            "cancel it before changing the phase"
        )
        pr_number = _qual_state.get("promotion_pr_number")
        if pr_number is not None:
            detail += f" (PR #{pr_number}"
            pr_url = _qual_state.get("promotion_pr_url")
            if pr_url:
                detail += f" {pr_url}"
            detail += ")"
        detail += _release_version.format_promotion_freshness(_qual_state)
        raise HTTPException(status_code=409, detail=detail)

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

    # Acquire the open version under FOR UPDATE lock, then re-check preconditions
    # against the freshly-locked row. Between the initial read and this lock,
    # a concurrent promotion/abandon could have changed the version's state.
    open_ver = (
        (
            await session.execute(
                select(ReleaseVersion)
                .where(
                    ReleaseVersion.project_id == project_id,
                    ReleaseVersion.status == "open",
                )
                .order_by(ReleaseVersion.opened_at.desc())
                .limit(1)
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
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
    # Client-generated idempotency key. When supplied, a repeat cut (double-click
    # / lost-response retry) carrying the same key returns the original cut's
    # result instead of abandoning the freshly-opened next version a second time,
    # mirroring the message and direction composers.
    idempotency_key: str | None = _Field(default=None, max_length=255)


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
    try:
        abandoned, new_version = await _release_version.abandon_current_version(
            session,
            project_id,
            reason=payload.reason,
            operator_id=operator_id,
            idempotency_key=payload.idempotency_key,
        )
    except _release_version.VersionCutBlockedByPromotion as exc:
        detail = (
            "a promotion PR is open and auto-merging — wait for it to land or "
            "cancel it before cutting"
        )
        if exc.promotion_pr_number is not None:
            detail += f" (PR #{exc.promotion_pr_number}"
            if exc.promotion_pr_url:
                detail += f" {exc.promotion_pr_url}"
            detail += ")"
        detail += _release_version.format_promotion_freshness(exc.promotion_freshness)
        raise HTTPException(status_code=409, detail=detail) from exc
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


class _SetPromotionEnabledBody(BaseModel):
    enabled: bool | None = _Field(default=None)


@router.post("/projects/{project_id}/promotion-enabled")
async def set_promotion_enabled(
    project_id: uuid.UUID,
    payload: _SetPromotionEnabledBody,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Arm, hold, or clear ONE project's per-project live-promotion override.

    Owner-gated. The request body is ``{"enabled": true | false | null}``:

    * ``true`` arms live promotion for THIS project, ``false`` holds it — either
      way the project's promotion gate consults that explicit value independently
      of the fleet-wide ``promotion_enabled`` and of every other project;
    * ``null`` CLEARS this project's override so it falls back to the fleet-wide
      default, restoring byte-identical behavior to a project with no override.

    The map is read-modify-written under ``runtime_config_lock`` so a concurrent
    edit to a DIFFERENT project is never clobbered, then persisted via the durable
    settings path (:func:`admin_settings._apply_settings`) so the change survives a
    Cloud Run restart.

    Response keys (additive-only): ``project_id``, ``override`` (the explicit
    per-project value now stored, or ``None`` when cleared), ``effective`` (the
    value the gate will consult after fleet fallback), ``source``
    (``"project"`` | ``"fleet"``), and ``cleared``.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    from app.routers.admin_settings import _apply_settings
    from app.shared_state import runtime_config, runtime_config_lock

    sid = str(project_id)
    async with runtime_config_lock:
        promo_map = dict(promotion_enabled_by_project_map())
        if payload.enabled is None:
            cleared = sid in promo_map
            promo_map.pop(sid, None)
        else:
            cleared = False
            promo_map[sid] = bool(payload.enabled)
        await _apply_settings(
            {"promotion_enabled_by_project": promo_map}, session, runtime_config
        )
        resulting = promotion_enabled_by_project_map()
        effective = resolve_project_promotion_enabled(project_id)

    return {
        "project_id": sid,
        "override": resulting.get(sid),
        "effective": effective,
        "source": "project" if sid in resulting else "fleet",
        "cleared": cleared,
    }


@router.get("/projects/{project_id}/audit-findings")
async def get_audit_findings(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return the latest qualification's audit findings for a project.

    Read-only; viewer+ role required. Prefers the durable per-attempt evidence
    on the latest immutable ``QualificationVerdict`` row for the open version
    (its ``audit_findings`` column survives re-qualification overwrites), and
    falls back to the mutable ``last_qualification.audit_findings`` blob only
    when no verdict row carries findings (e.g. legacy rows predating the
    evidence columns).  The ``source`` field reflects which store was used
    (``qualification_verdict`` vs ``last_qualification``).  Returns an empty
    list when no open version exists or no qualification has run yet.
    """
    from app.release_qualifier import (  # noqa: PLC0415
        get_latest_qualification_audit_findings,
    )

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

    # Prefer the latest durable verdict row that carries audit findings for this
    # version — it survives the next re-qualification's last_qualification
    # overwrite, so a blocked attempt's findings are not lost.
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
            "findings": get_latest_qualification_audit_findings(open_row, verdict),
            "audit_missing_reason": verdict.audit_missing_reason,
            "finalized_at": verdict.finalized_at.isoformat()
            if verdict.finalized_at
            else None,
            "source": "qualification_verdict",
        }

    qual = open_row.last_qualification or {}
    return {
        "findings": get_latest_qualification_audit_findings(open_row),
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
    from app.release_qualifier import (  # noqa: PLC0415
        compute_promotion_readiness,
        format_verdict_for_api,
    )

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
        "verdicts": [format_verdict_for_api(v) for v in rows],
        "total": len(rows),
        "infra_context": {
            "infra_failure_count": readiness.get("infra_failure_count", 0),
            "consecutive_infra_errors": readiness.get("consecutive_infra_errors", 0),
        },
    }


class _ClearDeployFailedBody(BaseModel):
    # Operator override: clear a stale deploy_failed even when the live serving
    # SHA is unobservable (the pre-Cloud-Run-API interim). Without it, the clear
    # only proceeds when the deploy is independently confirmed serving.
    force: bool = _Field(default=False)
    # Recorded attestation reason — REQUIRED for the force path so the durable
    # audit trail records WHY an unverifiable deploy was declared live.
    reason: str | None = _Field(default=None, max_length=500)


def _record_deploy_failed_clear_attestation(
    version: ReleaseVersion,
    operator_id: str,
    reason: str,
    now,
) -> None:
    """Stamp an operator-attested deploy_failed clear onto the version audit trail.

    The recovery helper clears ``deploy_failure_reason``; this records the durable
    provenance (operator identity, reason, timestamp, mode) of an attested clear
    that was NOT independently verified live, so the audit trail never silently
    loses the fact that a human declared the deploy serving. Reassigns
    ``last_qualification`` (rather than mutating in place) so SQLAlchemy detects
    the JSON change.
    """
    qual = dict(version.last_qualification or {})
    qual["deploy_failed_clear"] = {
        "mode": "operator_attested",
        "operator": operator_id,
        "reason": reason,
        "attested_at": now.isoformat(),
    }
    version.last_qualification = qual


@router.post("/projects/{project_id}/clear-deploy-failed")
async def clear_deploy_failed(
    project_id: uuid.UUID,
    payload: _ClearDeployFailedBody,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Acknowledge and clear a stale post-promotion ``deploy_failed`` serving state.

    Admin-gated (matching the deploy operator levers). Targets the latest
    ``promoted`` version whose ``serving_status`` is ``deploy_failed`` for the
    project, under a per-version ``with_for_update`` row lock.

    Behavior:

    * FIRST refresh the required-artifact registry. A clear is only legal when
      every REQUIRED non-backend artifact is already ``serving_confirmed``.
    * If the backend-revision artifact is independently observed serving, clear
      via the shared ``_recover_serving_confirmed`` helper (flip to
      serving_confirmed, stamp the observed served_sha/served_at, clear
      deploy_failure_reason, update the verdict outcome, resolve the alert, arm
      a strategic refresh).
    * If the backend serving SHA is unobservable or does not yet match (the
      pre-Cloud-Run-API interim), require an explicit ``force=true`` plus a
      recorded operator ``reason``, then clear via the SAME helper using the
      promoted SHA as the attested served SHA and record the operator
      attestation in the version audit trail. The force path only attests the
      backend-serving signal; it does NOT override any still-failed required
      non-backend artifact.
    * Without the override and without an independent confirmation, return 409
      explaining the deploy could not be independently verified live.

    Returns 404 when no promoted deploy_failed version exists for the project.
    """
    from app.release_reconciliation import (  # noqa: PLC0415
        _aggregate_non_backend_required_artifact_status,
        _recover_serving_confirmed,
        _refresh_release_artifact_rows,
    )
    from app.time_utils import _utcnow  # noqa: PLC0415

    version = (
        (
            await session.execute(
                select(ReleaseVersion)
                .where(
                    ReleaseVersion.project_id == project_id,
                    ReleaseVersion.status == "promoted",
                    ReleaseVersion.serving_status == "deploy_failed",
                )
                .order_by(ReleaseVersion.promoted_at.desc())
                .limit(1)
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if version is None:
        raise HTTPException(
            status_code=404,
            detail="No promoted version in deploy_failed state for this project.",
        )

    now = _utcnow()
    (
        provider_rows,
        _,
        _,
        backend_artifact,
    ) = await _refresh_release_artifact_rows(
        session,
        version,
        now,
        recheck_failed_required=True,
    )
    non_backend_status, non_backend_reason = (
        _aggregate_non_backend_required_artifact_status(provider_rows)
    )

    operator_id = current_user.get("email") or current_user["user_id"]

    if non_backend_status != "serving_confirmed":
        detail = (
            "deploy could not be cleared because a required non-backend artifact "
            "is not serving_confirmed"
        )
        if non_backend_reason:
            detail = f"{detail}: {non_backend_reason}"
        raise HTTPException(status_code=409, detail=detail)

    if (
        backend_artifact is not None
        and backend_artifact.serving_status == "serving_confirmed"
        and backend_artifact.served_identifier is not None
    ):
        # Independently confirmed serving — clear with the OBSERVED served SHA.
        await _recover_serving_confirmed(
            session,
            version,
            backend_artifact.served_identifier,
            backend_artifact.served_at,
            now,
        )
        clear_mode = "verified"
        attestation_reason = None
    else:
        # Live serving SHA is unobservable or does not yet match — refuse unless
        # the operator explicitly overrides AND records a reason.
        if not payload.force:
            raise HTTPException(
                status_code=409,
                detail=(
                    "deploy could not be independently verified live — the promoted "
                    "SHA is not observed serving. Re-submit with force=true and a "
                    "reason to attest the deploy is live."
                ),
            )
        attestation_reason = (payload.reason or "").strip()
        if not attestation_reason:
            raise HTTPException(
                status_code=400,
                detail="A force clear requires a non-empty operator reason.",
            )
        # Operator attestation: clear using the promoted SHA as the served SHA and
        # record the provenance in the durable audit trail.
        await _recover_serving_confirmed(
            session, version, version.promoted_sha, now, now
        )
        _record_deploy_failed_clear_attestation(
            version, operator_id, attestation_reason, now
        )
        clear_mode = "operator_attested"

    await session.commit()
    await session.refresh(version)
    return {
        "version_number": version.version_number,
        "serving_status": version.serving_status,
        "served_sha": version.served_sha,
        "served_at": version.served_at.isoformat() if version.served_at else None,
        "deploy_failure_reason": version.deploy_failure_reason,
        "clear_mode": clear_mode,
        "operator_attested": clear_mode == "operator_attested",
        "attestation_reason": attestation_reason,
    }
