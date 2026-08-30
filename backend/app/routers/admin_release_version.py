"""Release version lifecycle endpoints."""

import asyncio
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel, Field as _Field
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only, selectinload
from sqlmodel import select

from app.database import get_session
from app.models import (
    ReleaseQualificationIntent,
    ReleaseVersion,
    Project,
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
from app.release_artifacts import (
    is_activation_rearmable_serving_status,
    version_serving_summary as _version_serving_summary,
)
from app.release_activation_approval import (
    ActivationApprover,
    approve_latest_promoted_activation,
)
from app.release_authority_lock import lock_release_project
from app.qualification_state_resolver import (
    resolve_effective_qualification_state,
    resolve_newest_qualification_verdict,
    resolve_open_version_qualification_read_model,
    resolve_promotion_blocking_result,
    resolve_dimension_states,
    resolve_dimension_states_from_ledger,
    patch_qualification_state_from_dimension_states,
)
from app.time_utils import _coerce_naive_utc, utc_iso
from app import release_version as _release_version
from app import compute as _compute
from app.digest_cache import DigestCache

router = APIRouter()

logger = logging.getLogger(__name__)

# Short-window caches for dashboard-polled endpoints.
# Keyed by project_id (not user) — access check runs before every cache lookup.
# agent-serving-shas: GitHub branch SHA lookups change infrequently; 5-min TTL
# prevents quota burn on the ~30s poll cadence.
_agent_serving_shas_cache = DigestCache(default_ttl=300.0, stale_retry_ttl=60.0)
# version-lifecycle: multi-step DB computation polled every 30s+; 30s TTL keeps
# it fresh enough while collapsing burst polls into a single DB round-trip.
_version_lifecycle_cache = DigestCache(default_ttl=30.0, stale_retry_ttl=15.0)

_MAX_RETIREMENT_CRITIQUES = 100


def _qualification_intent_projection(
    row: ReleaseQualificationIntent,
) -> dict[str, Any]:
    """Bounded exact receipt projection for qualification delivery recovery."""

    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "release_version_id": str(row.release_version_id),
        "source_task_id": str(row.source_task_id),
        "authority_kind": row.authority_kind,
        "authority_key": row.authority_key,
        "idempotency_key": row.idempotency_key,
        "specification_hash": row.specification_hash,
        "reason": row.reason,
        "requested_probes": list(row.requested_probes or []),
        "expected_phase_changed_at": utc_iso(row.expected_phase_changed_at),
        "status": row.status,
        "attempt_count": int(row.attempt_count or 0),
        "claim_id": str(row.claim_id) if row.claim_id is not None else None,
        "lease_expires_at": utc_iso(row.lease_expires_at),
        "next_attempt_at": utc_iso(row.next_attempt_at),
        "result": dict(row.result or {}),
        "last_error": row.last_error,
        "created_at": utc_iso(row.created_at),
        "updated_at": utc_iso(row.updated_at),
        "completed_at": utc_iso(row.completed_at),
    }


def _compose_serving_block(
    aggregate: dict,
    artifacts: list,
    version: "ReleaseVersion",
) -> dict:
    """Build the resolved serving block from aggregate + artifact evidence.

    Mirrors system_state.compute_system_state so every read site resolves
    serving state identically: sha from the first confirmed artifact
    (fallback to the legacy scalar), confirmed_at from that same artifact,
    failure_reason only when the aggregate status is deploy_failed.
    """
    aggregate_status = aggregate.get("serving_status")
    serving_sha = next(
        (
            e["served_identifier"]
            for e in artifacts
            if e.get("serving_status") == "serving_confirmed"
            and e.get("served_identifier") is not None
        ),
        getattr(version, "served_sha", None),
    )
    confirmed_at = next(
        (
            e["served_at"]
            for e in artifacts
            if e.get("serving_status") == "serving_confirmed" and e.get("served_at")
        ),
        utc_iso(version.served_at),
    )
    return {
        "status": aggregate_status,
        "sha": serving_sha,
        "confirmed_at": confirmed_at,
        "failure_reason": aggregate.get("reason_code")
        if aggregate_status == "deploy_failed"
        else None,
        "fleet_freshness": aggregate.get("fleet_freshness"),
        "artifacts": artifacts,
    }


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
                raiseload=True,
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
                    "cycle_created_at": utc_iso(cycle.created_at),
                }
            )
            if len(critiques_out) >= limit:
                break

    return {"critiques": critiques_out, "total": len(critiques_out)}


def _format_qualification(qual: Dict[str, Any]) -> Dict[str, Any]:
    """Extract operator-facing fields from a last_qualification record."""
    from app.release_readiness import format_qualification_for_api  # noqa: PLC0415

    out = format_qualification_for_api(qual)
    candidate_gate = qual.get("candidate_operator_authorship_gate")
    if not isinstance(candidate_gate, dict):
        sub_scores = qual.get("sub_scores")
        candidate_gate = (
            sub_scores.get("candidate_operator_authorship")
            if isinstance(sub_scores, dict)
            else None
        )
    out.update(
        {
            "status": qual.get("status"),
            "qualification_attempt_id": qual.get("qualification_attempt_id"),
            "retry_episode": qual.get("retry_episode"),
            "retry_episode_cap": qual.get("retry_episode_cap"),
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
            # Candidate-range authorship is a promotion gate, not the
            # historical serving receipt. Preserve its explicit unconfigured
            # state and null budget so the operator can declare policy.
            "candidate_operator_authorship_gate": candidate_gate,
            # Infra-failure context (populated only when status=infra_failure).
            "error": qual.get("error"),
            "consecutive_infra_errors": qual.get("consecutive_infra_errors"),
            # True when the same migration-apply gate failure has repeated
            # _MIN_INFRA_ERROR_STREAK+ times with an identical signature —
            # the operator hotfix-deploy channel is the sanctioned remedy.
            "is_permanent_gate_incapacity": qual.get("is_permanent_gate_incapacity"),
            # Audit observability: why the audit sub-score is missing/unscoreable.
            "audit_missing_reason": qual.get("audit_missing_reason"),
            # Audit reconciliation: model-reported vs findings-derived score delta
            # and excluded findings count (populated by _compute_audit_reconciliation).
            "audit_reconciliation": qual.get("audit_reconciliation"),
        }
    )
    return out


def _closed_version_until(version: ReleaseVersion) -> Optional[datetime]:
    """Return the upper-bound timestamp for a closed version's merged-task window.

    For promoted versions returns ``promoted_at``.  For abandoned versions
    returns the ``abandoned_at`` ISO string stored in ``last_qualification``.
    Returns ``None`` for open versions (unbounded window).
    """
    if version.status == "promoted" and version.promoted_at is not None:
        return version.promoted_at
    if version.status == "abandoned":
        lq = version.last_qualification or {}
        raw = lq.get("abandoned_at")
        if isinstance(raw, str):
            try:
                # _coerce_naive_utc converts any stored offset (including a
                # 'Z'-suffix from a tz-aware writer) to naive UTC rather than
                # dropping it, keeping the window bound consistent with the
                # naive-UTC timestamps the merged-task queries compare against.
                return _coerce_naive_utc(datetime.fromisoformat(raw))
            except (ValueError, TypeError):
                pass
    return None


async def _serialize_version_history_entry(
    version: ReleaseVersion,
    project: Optional[Any] = None,
    *,
    session: Optional[AsyncSession] = None,
    project_id: Optional[uuid.UUID] = None,
) -> dict[str, Any]:
    from app.release_version import (  # noqa: PLC0415
        count_merged_since,
        compute_version_acceptance_aggregate,
    )

    aggregate, artifacts = _version_serving_summary(version, project)
    qualification_state, _ = resolve_effective_qualification_state(
        version, project, serving=(aggregate, artifacts)
    )
    # Defer dimension_states computation so we can patch qualification_state below.

    # Enrich each history entry with the same server-computed highlights and
    # merged counts the per-version summary endpoint produces, bounded by the
    # closed-version window so later-version merges are not double-counted.
    highlights: Optional[dict[str, Any]] = None
    merged_count: Optional[int] = None
    merged_task_count: Optional[int] = None
    acceptance_aggregate: Optional[dict[str, Any]] = None
    criterion_coverage: Optional[dict[str, Any]] = None
    if session is not None and project_id is not None:
        version_until = _closed_version_until(version)
        merged_count = await count_merged_since(
            session, project_id, version.opened_at, until=version_until
        )
        merged_task_count = merged_count
        acceptance_aggregate = await compute_version_acceptance_aggregate(
            session, project_id, since=version.opened_at, until=version_until
        )
        highlights = _release_version.build_version_highlights(
            version,
            merged_count=merged_count,
            aggregate_serving_status=aggregate["serving_status"],
        )
        try:
            from app import acceptance_criteria as _acceptance_criteria  # noqa: PLC0415

            criterion_coverage = await _acceptance_criteria.get_version_coverage_summary(
                session, version.id
            )
        except Exception:
            logger.exception(
                "[VERSION_HISTORY] criterion coverage compute failed for version %s",
                version.id,
            )

    # Compute dimension_states from the ledger when possible, then patch
    # qualification_state.sub_scores so both surfaces tell one consistent story.
    _dim_states: list[Any] = (
        await resolve_dimension_states_from_ledger(session, version)
        if session is not None
        else resolve_dimension_states(version.last_qualification)
    )
    qualification_state = patch_qualification_state_from_dimension_states(
        qualification_state, _dim_states
    )

    return {
        "id": str(version.id),
        "version_number": version.version_number,
        "status": version.status,
        "phase": version.phase,
        "opened_at": utc_iso(version.opened_at),
        "promoted_at": utc_iso(version.promoted_at),
        "promoted_sha": version.promoted_sha,
        "qualification_attempts": version.qualification_attempts,
        "qualification_retry_episode": getattr(version, "qualification_retry_episode", 0),
        "active_qualification_attempt_id": (
            str(version.active_qualification_attempt_id)
            if getattr(version, "active_qualification_attempt_id", None)
            else None
        ),
        "last_qualification": _format_qualification(version.last_qualification)
        if version.last_qualification
        else None,
        # Post-promotion serving confirmation — disambiguates a merged
        # promotion from one that actually shipped (additive fields).
        "serving_status": version.serving_status,
        "served_sha": version.served_sha,
        "served_at": utc_iso(version.served_at),
        "deploy_failure_reason": version.deploy_failure_reason,
        "deploy_confirm_deadline": utc_iso(version.deploy_confirm_deadline),
        # Approved-activation control state (additive; NULL until an operator
        # approves activation).  activation_requested_at NULL + unconfirmed is
        # "awaiting approval"; set + unconfirmed is "approved-but-not-yet-executed".
        "activation_requested_at": utc_iso(version.activation_requested_at),
        "activation_requested_by": version.activation_requested_by,
        "aggregate_serving_status": aggregate["serving_status"],
        "aggregate_deploy_failure_reason": aggregate["reason_code"],
        "artifacts": artifacts,
        "serving": _compose_serving_block(aggregate, artifacts, version),
        "qualification_state": qualification_state,
        "abandoned_at": (
            (version.last_qualification or {}).get("abandoned_at")
            if version.status == "abandoned"
            else None
        ),
        "abandoned_by": (
            (version.last_qualification or {}).get("operator")
            if version.status == "abandoned"
            else None
        ),
        "abandon_reason": (
            (version.last_qualification or {}).get("reason")
            if version.status == "abandoned"
            else None
        ),
        # Out-of-band promotion provenance: non-null when an operator force-merged
        # this version outside the normal qualification flow. Degrades gracefully
        # when the model column is absent (sibling PR not yet on this branch).
        "out_of_band_promotion": getattr(version, "out_of_band_promotion", None),
        # Server-computed highlights (headline + metrics) — additive; null when
        # session/project_id not provided (older callers degrade gracefully).
        "highlights": highlights,
        # Merged-task count for this version's window (both keys for consistency:
        # current_version uses merged_task_count, summary endpoint uses merged_count).
        "merged_count": merged_count,
        "merged_task_count": merged_task_count,
        "acceptance_aggregate": acceptance_aggregate,
        # Same shape as current_version. total/covered/partial/uncovered count
        # active criteria only; inactive audit rows may still appear in criteria.
        "criterion_coverage": criterion_coverage,
        # Per-dimension qualification state — additive, same shape as current_version.
        # When a DB session is available, prefer the QualificationDimensionResult ledger
        # (which survives re-qualification overwrites); otherwise fall back to the
        # mutable last_qualification blob.
        "dimension_states": _dim_states,
    }


async def _best_effort_branch_sha(
    project: Project | None,
    branch: str | None,
) -> str | None:
    if not project or not branch or not project.github_owner or not project.github_repo:
        return None
    try:
        from app.github import get_repo_installation_token  # noqa: PLC0415
        from app.github_refs import get_branch_sha  # noqa: PLC0415

        token = await get_repo_installation_token(
            project.github_owner,
            project.github_repo,
        )
        if not token:
            return None
        return await get_branch_sha(
            token,
            project.github_owner,
            project.github_repo,
            branch,
            project_id=str(project.id),
        )
    except Exception:
        return None


@router.get("/projects/{project_id}/agent-serving-shas")
async def get_agent_serving_shas(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return observed agent-image source SHAs and release drift context.

    Read-only operator provenance: combines durable warm-pool heartbeat rows
    with in-memory task-VM heartbeat caches, plus best-effort current image and
    branch metadata. Missing external data degrades to ``null`` fields.

    GitHub branch SHA lookups are cached for 5 minutes (TTL) to avoid quota
    burn on the ~30-second dashboard poll cadence. Fleet/DB data is always
    fresh per request.
    """
    project = await _get_user_project_with_role(
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
    served_row = (
        (
            await session.execute(
                select(ReleaseVersion)
                .where(
                    ReleaseVersion.project_id == project_id,
                    ReleaseVersion.served_sha.isnot(None),
                )
                .order_by(ReleaseVersion.served_at.desc().nullslast())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )

    from app.branch_resolution import (  # noqa: PLC0415
        get_project_integration_branch,
        get_project_promotion_target,
    )

    integration_branch = get_project_integration_branch(project)
    promotion_target = get_project_promotion_target(project)

    # Cache GitHub branch SHA lookups keyed by (project_id, branch).
    # In-flight dedup + stale-on-failure prevents quota bursts and 500s.
    async def _cached_integration_sha() -> str | None:
        return await _best_effort_branch_sha(project, integration_branch)

    async def _cached_promotion_sha() -> str | None:
        return await _best_effort_branch_sha(project, promotion_target)

    integration_head_sha, promotion_head_sha, current_image = await asyncio.gather(
        _agent_serving_shas_cache.get_or_compute(
            ("integration_sha", str(project_id), integration_branch),
            _cached_integration_sha,
        ),
        _agent_serving_shas_cache.get_or_compute(
            ("promotion_sha", str(project_id), promotion_target),
            _cached_promotion_sha,
        ),
        asyncio.to_thread(_compute.get_current_agent_image),
    )
    fleet = await _compute.agent_fleet_source_sha_snapshot(session, project_id)

    active_candidate_sha = None
    if open_row is not None and isinstance(open_row.last_qualification, dict):
        active_candidate_sha = open_row.last_qualification.get("candidate_sha")

    backend_served_sha = served_row.served_sha if served_row is not None else None
    current_image_sha = current_image.get("source_git_sha") if current_image else None
    for bucket in fleet["buckets"]:
        sha = bucket.get("source_sha")
        bucket["matches_backend_served"] = bool(sha and sha == backend_served_sha)
        bucket["matches_active_candidate"] = bool(sha and sha == active_candidate_sha)
        bucket["matches_integration_head"] = bool(sha and sha == integration_head_sha)
        bucket["matches_current_image"] = bool(sha and sha == current_image_sha)

    return {
        "fleet": fleet,
        "current_image": current_image,
        "backend_served_sha": backend_served_sha,
        "active_candidate_sha": active_candidate_sha,
        "integration_branch": integration_branch,
        "integration_head_sha": integration_head_sha,
        "promotion_target": promotion_target,
        "promotion_head_sha": promotion_head_sha,
    }


@router.get("/projects/{project_id}/qualification-intents")
async def get_release_qualification_intents(
    project_id: uuid.UUID,
    intent_id: uuid.UUID | None = Query(default=None),
    release_version_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return uncached durable qualification delivery receipts."""

    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="viewer"
    )
    statement = select(ReleaseQualificationIntent).where(
        ReleaseQualificationIntent.project_id == project_id
    )
    if intent_id is not None:
        statement = statement.where(ReleaseQualificationIntent.id == intent_id)
    if release_version_id is not None:
        statement = statement.where(
            ReleaseQualificationIntent.release_version_id == release_version_id
        )
    rows = (
        (
            await session.execute(
                statement.order_by(
                    ReleaseQualificationIntent.created_at.desc()
                ).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if intent_id is not None and not rows:
        raise HTTPException(status_code=404, detail="Qualification intent not found")
    return {
        "qualification_intents": [
            _qualification_intent_projection(row) for row in rows
        ],
        "count": len(rows),
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

    The lifecycle body is cached per project for 30 seconds (in-flight dedup +
    stale fallback) to bound the DB work from dashboard polling at ~30s cadence.
    Volatile promotion-policy truth is overlaid after the cache lookup so a live
    runtime/project override is visible immediately. ``precomputed`` in the
    response is ``true`` when a cached value was served.
    """
    from app.release_version import (  # noqa: PLC0415
        count_merged_since,
        count_in_flight_tasks,
        list_merged_tasks_for_version,
    )
    from app.release_qualifier import (  # noqa: PLC0415
        compute_promotion_readiness,
        current_effective_policy_projection,
        resolve_lifecycle_audit_evidence,
    )
    from app.strategic_spawning import (  # noqa: PLC0415
        _build_staged_directions_summary,
    )

    project_row = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="viewer"
    )

    async def _compute_lifecycle() -> Dict[str, Any]:
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

        # Re-qualification readiness signal (ledger-backed) for the open version.
        # Surfaced ADDITIVELY on current_version_out so the version-lifecycle UI can
        # show whether the pre-registered re-qualification trigger (every gating
        # high+medium carried finding resolved-or-stale) is met, plus the per-finding
        # resolution state. Best-effort: a failure computing it must never break the
        # lifecycle read — it degrades to ``requalification_ready: None``.
        requalification_readiness: Optional[Dict[str, Any]] = None
        if open_row is not None:
            try:
                from app.release_readiness import (  # noqa: PLC0415
                    compute_requalification_readiness,
                )

                requalification_readiness = await compute_requalification_readiness(
                    session, project_id
                )
            except Exception:
                logger.exception(
                    "[VERSION_LIFECYCLE] requalification readiness compute failed "
                    "for project %s; serving lifecycle without the signal",
                    project_id,
                )

        criterion_coverage: Optional[Dict[str, Any]] = None
        if open_row is not None:
            try:
                from app import acceptance_criteria as _acceptance_criteria  # noqa: PLC0415

                criterion_coverage = await _acceptance_criteria.get_version_coverage_summary(
                    session, open_row.id
                )
            except Exception:
                logger.exception(
                    "[VERSION_LIFECYCLE] criterion coverage compute failed "
                    "for project %s; serving lifecycle without the signal",
                    project_id,
                )

        # Full-suite drift-sentinel CI gate (ADDITIVE, read-only).  Surfaces the
        # latest ci.yml run state for the integration-branch HEAD so the admin UI
        # can show full-suite status without waiting for a promotion PR. dispatch=False
        # so this read NEVER triggers a workflow run; the sweep sentinel dispatches.
        full_suite_ci_gate: Optional[Dict[str, Any]] = None
        if open_row is not None:
            try:
                from app.release_readiness import (  # noqa: PLC0415
                    compute_promotion_ci_green_gate,
                )

                full_suite_ci_gate = await compute_promotion_ci_green_gate(
                    session, project_id, dispatch=False
                )
            except Exception:
                logger.exception(
                    "[VERSION_LIFECYCLE] full-suite CI gate compute failed "
                    "for project %s; serving lifecycle without the signal",
                    project_id,
                )

        # Qualification trigger explain-surface (additive, read-only).  Answers
        # 'why hasn't qualification fired' for the open version.  Best-effort: a
        # failure computing it must never break the lifecycle read.
        _qualification_trigger_state: Optional[Dict[str, Any]] = None
        if open_row is not None:
            try:
                from app import category_verdict_ledger as _cvl  # noqa: PLC0415
                from app.qualification_triggers import (  # noqa: PLC0415
                    explain_qualification_trigger as _explain_trigger,
                )
                from app.release_readiness import (  # noqa: PLC0415
                    compute_requalification_readiness as _compute_readiness,
                )
                from app.runtime_config_state import (  # noqa: PLC0415
                    get_auto_qualification_triggers_mode as _get_trigger_mode,
                )
                from app.time_utils import _utcnow  # noqa: PLC0415

                _lq = open_row.last_qualification or {}
                _lq_status = _lq.get("status")
                _is_candidate_fully_met = await _cvl.is_candidate_fully_met(
                    session, open_row.id, _lq.get("candidate_sha")
                )
                _rdns = await _compute_readiness(session, project_id)
                _qualification_trigger_state = _explain_trigger(
                    phase=open_row.phase or "",
                    status=_lq_status,
                    error_code=_lq.get("error") if _lq_status == "error" else None,
                    qualification_attempts=open_row.qualification_attempts or 0,
                    error_redrive_count=int(_lq.get("error_redrive_count") or 0),
                    is_candidate_fully_met=_is_candidate_fully_met,
                    requalification_ready=_rdns.get("requalification_ready"),
                    candidate_sha=_lq.get("candidate_sha"),
                    automain_sha=None,  # not fetched in the read path
                    automain_ci_state=None,
                    blocker=_lq.get("blocker"),
                    in_flight=_lq_status == "pending",
                    promotion_check_wait=bool(_lq.get("promotion_pending")),
                    rubric_redrive_count=int(_lq.get("rubric_redrive_count") or 0),
                    promotion_failure_count=int(
                        _lq.get("promotion_failure_count") or 0
                    ),
                    last_attempt=_lq if _lq else None,
                    knob=_get_trigger_mode(),
                    evaluated_at=utc_iso(_utcnow()),
                )
            except Exception:
                logger.exception(
                    "[VERSION_LIFECYCLE] qualification_trigger_state compute failed "
                    "for project %s; serving lifecycle without the trigger signal",
                    project_id,
                )

        # Detected deployed-gate latch (additive, read-only). Reuses the Alert row
        # the reaper sweep already persisted when a blocked candidate carries the
        # repair to the DEPLOYED implementation of its own blocking gate — a
        # cleanly-run qualification returning a scoring blocker never sets
        # is_permanent_gate_incapacity (that flag only fires on a same-signature
        # INFRA-error streak), so this is the only signal that surfaces the
        # hotfix lane for a latched scoring gate. Best-effort: a failure here
        # must never break the lifecycle read.
        gate_latch: Optional[Dict[str, Any]] = None
        if open_row is not None:
            try:
                from app.release_gate_latch import (  # noqa: PLC0415
                    resolve_active_gate_latch_alert,
                )

                gate_latch = await resolve_active_gate_latch_alert(
                    session, open_row.id
                )
            except Exception:
                logger.exception(
                    "[VERSION_LIFECYCLE] gate latch resolve failed "
                    "for project %s; serving lifecycle without the signal",
                    project_id,
                )

        current_version_out: Optional[Dict[str, Any]] = None
        if open_row is not None:
            merged_count = await count_merged_since(
                session, project_id, open_row.opened_at
            )
            in_flight_count = await count_in_flight_tasks(session, project_id)
            _tasks_merged_all = await list_merged_tasks_for_version(
                session, project_id, open_row.opened_at
            )
            _TASKS_MERGED_LIMIT = 50
            tasks_merged = _tasks_merged_all[:_TASKS_MERGED_LIMIT]
            tasks_merged_has_more = len(_tasks_merged_all) > _TASKS_MERGED_LIMIT
            qual = open_row.last_qualification or {}
            aggregate_serving, artifact_evidence = _version_serving_summary(
                open_row, project_row
            )
            acceptance_aggregate = None
            try:
                from app.release_version import (  # noqa: PLC0415
                    compute_version_acceptance_aggregate,
                )

                acceptance_aggregate = await compute_version_acceptance_aggregate(
                    session,
                    project_id,
                    since=open_row.opened_at,
                )
            except Exception:
                logger.exception(
                    "[VERSION_LIFECYCLE] acceptance aggregate compute failed "
                    "for project %s; serving lifecycle with the stored fallback",
                    project_id,
                )
                if isinstance(qual, dict):
                    acceptance_aggregate = qual.get("acceptance_aggregate")
                if acceptance_aggregate is None:
                    acceptance_aggregate = getattr(
                        open_row, "acceptance_aggregate", None
                    )
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
            # Feed the version-scoped audit child into the qualification-state summary
            # so a crashed audit (terminal-failed, no report) reconciles to
            # ``audit_crashed`` instead of a misleading ``qualifying``/'wait' — the top
            # badge and the audit-findings section then agree by construction.
            # Routed through the shared OPEN-version resolver so this endpoint ALSO
            # applies the finalized-verdict substitution (stale-pending window) that
            # it previously omitted: reuse the ``latest_audit`` already resolved above
            # for the audit-findings section, and let the helper backfill the newest
            # verdict so all four open-version surfaces agree.
            # Resolve the ledger-backed dimension_states FIRST (also surfaced as
            # this endpoint's own ``dimension_states`` response field below), then
            # thread it into the seam so the shared ledger sub_score overlay is
            # applied without a duplicate ledger query. The seam now GUARANTEES the
            # overlay, so no separate patch call is needed here.
            _qualification_read = await resolve_open_version_qualification_read_model(
                session,
                open_row,
                project_row,
                audit_child=latest_audit,
                serving=(aggregate_serving, artifact_evidence),
            )
            _open_dim_states = _qualification_read.dimension_states
            qual_state = _qualification_read.qualification_state
            if _qualification_trigger_state is not None:
                _qualification_trigger_state["qualification_provenance"] = (
                    _qualification_read.provenance
                )

            # Fetch live current_focus from the project's problem_model
            live_current_focus = None
            if project_row and project_row.problem_model:
                intent = project_row.problem_model.get("intent", {})
                live_current_focus = intent.get("current_focus")

            current_version_out = {
                "version_number": open_row.version_number,
                "phase": open_row.phase,
                "phase_changed_at": utc_iso(open_row.phase_changed_at),
                "phase_reasoning": open_row.phase_reasoning,
                "goal": open_row.goal,
                "goal_updated_at": utc_iso(open_row.goal_updated_at),
                "goal_editor_info": open_row.goal_editor_info,
                "live_current_focus": live_current_focus,
                "opened_at": utc_iso(open_row.opened_at),
                "qualification_attempts": open_row.qualification_attempts,
                "qualification_retry_episode": open_row.qualification_retry_episode,
                "active_qualification_attempt_id": (
                    str(open_row.active_qualification_attempt_id)
                    if open_row.active_qualification_attempt_id
                    else None
                ),
                "qualification_provenance": _qualification_read.provenance,
                "merged_task_count": merged_count,
                # Version-window acceptance aggregate (proven-vs-merged) — additive
                # and nullable, surfaced ALONGSIDE the raw ``merged_task_count`` (not
                # replacing it). Computed/stored by the acceptance layer; ``None`` on
                # legacy open versions that predate the aggregate.
                "acceptance_aggregate": acceptance_aggregate,
                # Human-readable what's-new headline + at-a-glance metrics, built by
                # the SAME shared helper the per-version summary endpoint uses so the
                # current (open) version renders an identical headline. Additive and
                # derived from data already loaded above — no extra I/O. The 'serving'
                # word in the headline derives from the aggregate serving status, not
                # the scalar serving_status column.
                "highlights": _release_version.build_version_highlights(
                    open_row,
                    merged_count=merged_count,
                    aggregate_serving_status=aggregate_serving["serving_status"],
                ),
                # Tasks merged AT/AFTER the last qualification verdict's finalized_at
                # (verdict-staleness signal). None when no verdict/finalized_at.
                "merged_since_verdict": merged_since_verdict,
                # In-flight (non-terminal) tasks that a version cut will NOT stop —
                # they keep running and land against the freshly-opened next version.
                # Surfaced so the cut confirmation can disclose this consequence.
                "in_flight_task_count": in_flight_count,
                "qualification_state": qual_state,
                "last_qualification": _format_qualification(qual) if qual else None,
                # Ledger-backed re-qualification readiness (ADDITIVE; nullable when
                # the signal could not be computed). ``requalification_ready`` is the
                # top-level boolean trigger; ``requalification_readiness`` carries the
                # full per-finding resolution view (each gating finding's key,
                # severity, summary, and resolved|stale|unresolved state) plus the
                # gating counts and human-readable reason.
                "requalification_ready": (
                    requalification_readiness.get("requalification_ready")
                    if requalification_readiness is not None
                    else None
                ),
                "requalification_readiness": requalification_readiness,
                # The candidate commit currently under evaluation for this open
                # version, surfaced as a first-class field (not buried in
                # last_qualification) so the UI can show what is being qualified and,
                # when audit evidence is stale, contrast it against the historical
                # audited candidate. Identical to the resolver's ``evaluated_sha``
                # (``last_qualification['candidate_sha']``) — same single source.
                "active_candidate_sha": qual.get("candidate_sha"),
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
                "served_at": utc_iso(open_row.served_at),
                "deploy_failure_reason": open_row.deploy_failure_reason,
                # Approved-activation control state (additive; normally NULL for a
                # still-open version — populated once an operator approves activation
                # of the promoted row this version becomes).
                "activation_requested_at": utc_iso(open_row.activation_requested_at),
                "activation_requested_by": open_row.activation_requested_by,
                "aggregate_serving_status": aggregate_serving["serving_status"],
                "aggregate_deploy_failure_reason": aggregate_serving["reason_code"],
                "artifacts": artifact_evidence,
                "serving": _compose_serving_block(
                    aggregate_serving, artifact_evidence, open_row
                ),
                "deploy_confirm_deadline": utc_iso(open_row.deploy_confirm_deadline),
                # Out-of-band promotion provenance (nullable). Non-null only when an
                # operator force-merged this version outside normal qualification flow.
                # Degrades gracefully when the model column is absent on this branch.
                "out_of_band_promotion": getattr(
                    open_row, "out_of_band_promotion", None
                ),
                # Per-task changelog for this version window: type, title, and PR
                # number for each merged task, newest-first. Allows operators to see
                # exactly what shipped without manual release notes.
                "tasks_merged": tasks_merged,
                # True when the version has more than _TASKS_MERGED_LIMIT merged
                # tasks; the UI can show a "showing N of total" indicator.
                "tasks_merged_has_more": tasks_merged_has_more,
                # Version-scoped acceptance criterion coverage (additive; nullable
                # when the signal could not be computed). Active totals are kept
                # separate from the audit trail: total/covered/partial/uncovered
                # count active criteria only, while inactive criteria can still
                # appear in criteria with status=retracted|superseded and
                # inactive lifecycle counts.
                "criterion_coverage": criterion_coverage,
                # Full-suite drift-sentinel CI gate (additive; nullable when the
                # gate could not be computed or project lacks GitHub creds).
                # Shows the latest ci.yml run state for the integration-branch HEAD
                # so operators can see full-suite health without a promotion PR.
                "full_suite_ci_gate": full_suite_ci_gate,
                # Event-driven qualification trigger explain-surface (additive;
                # nullable when compute fails). Answers 'why hasn't qualification
                # fired / what will fire it next' per design doc efbe4182.
                "qualification_trigger_state": _qualification_trigger_state,
                # Per-dimension qualification state derived from last_qualification.
                # Always a list (empty when no qualification has run); each entry
                # has key, label, status, score, reason. Additive — never breaks
                # older clients that ignore unknown fields.
                # Prefers the QualificationDimensionResult ledger when populated.
                # Already resolved above so qualification_state sub_scores could
                # be patched to match — both surfaces share one source of truth.
                "dimension_states": _open_dim_states,
                # Detected deployed-gate latch (additive; nullable). Distinct from
                # last_qualification.is_permanent_gate_incapacity: that flag only
                # fires on a same-signature INFRA-error streak, so a clean
                # qualification blocked by a scoring gate the candidate itself
                # repairs (in the DEPLOYED module) would otherwise never reveal
                # the hotfix lane. See app.release_gate_latch.
                "gate_latch": gate_latch,
            }

        history_rows = (
            (
                await session.execute(
                    select(ReleaseVersion)
                    .options(selectinload(ReleaseVersion.deploy_artifacts))
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

        from app.release_readiness import (  # noqa: PLC0415
            get_challenged_vs_unchallenged_telemetry,
        )

        challenged_vs_unchallenged = await get_challenged_vs_unchallenged_telemetry(
            session, project_id
        )

        recent_versions = []
        for version in history_rows:
            recent_versions.append(
                await _serialize_version_history_entry(
                    version,
                    project_row,
                    session=session,
                    project_id=project_id,
                )
            )

        return {
            "current_version": current_version_out,
            "recent_versions": recent_versions,
            "promotion_enabled": bool(_runtime_config.get("promotion_enabled", False)),
            # Per-project live-promotion enablement. ``promotion_enabled_effective``
            # is the value the promotion gate actually consults for THIS project (the
            # override when set, else the fleet default). ``promotion_enabled_override``
            # is the explicit per-project value (None when the project rides the fleet
            # default), and ``promotion_enabled_source`` tells the operator which one
            # is in effect.
            "promotion_enabled_effective": resolve_project_promotion_enabled(
                project_id
            ),
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
            "challenged_vs_unchallenged": challenged_vs_unchallenged,
        }

    result, served_stale = await _version_lifecycle_cache.get_or_compute_meta(
        str(project_id),
        _compute_lifecycle,
    )
    current_effective_policy = current_effective_policy_projection(project_row)
    return {
        **result,
        "current_effective_policy": current_effective_policy,
        "promotion_policy_source": current_effective_policy["source"],
        "promotion_policy_error": current_effective_policy["fallback_error"],
        "precomputed": served_stale,
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

    # Refuse a phase flip while Blueprint owns a managed promotion. Flipping
    # back to build here clears the promotion-pending indication even though
    # nothing was cancelled, leaving the operator levers disagreeing about the
    # version's real state. Mirrors the version-cut lever's 409 refusal on the same
    # predicate. (Only the operator endpoint guards this — the internal
    # set_version_phase helper stays open for finalize/sweep/promotion flows that
    # legitimately flip phase mid-promotion.)
    # Re-check against the LOCKED row with finalized-verdict substitution so a
    # stale-pending+finalized-verdict is not misclassified as still-blocking.
    _promo_state = await resolve_promotion_blocking_result(session, open_ver)
    if _promo_state is not None:
        pr_number = _promo_state.get("promotion_pr_number")
        if pr_number is not None:
            detail = (
                "a managed promotion PR is open — Blueprint will wait for required "
                "checks, revalidate the exact base/head pair, and retry the merge; "
                "wait for it to land or cancel it before changing the phase"
            )
        else:
            detail = (
                "a managed promotion is starting — wait for Blueprint to finish "
                "opening the promotion PR, or cancel it before changing the phase"
            )
        if pr_number is not None:
            detail += f" (PR #{pr_number}"
            pr_url = _promo_state.get("promotion_pr_url")
            if pr_url:
                detail += f" {pr_url}"
            detail += ")"
        detail += _release_version.format_promotion_freshness(_promo_state)
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
        "phase_changed_at": utc_iso(open_ver.phase_changed_at),
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
    _release_version.set_version_goal(open_ver, payload.goal, editor_id=operator_id)
    # This endpoint is the escape hatch named in the goal-omission alert's own
    # "TO RESOLVE" line, so taking it must end the condition. set_version_goal
    # cleared the streak counter; retire the durable alert row too — its
    # SOURCE_CONDITION retirement mode means no sweep will ever do it for us.
    await _release_version.resolve_version_goal_omission_alert(session, open_ver)
    session.add(open_ver)
    await session.commit()
    await session.refresh(open_ver)
    return {
        "version_number": open_ver.version_number,
        "goal": open_ver.goal,
        "goal_updated_at": utc_iso(open_ver.goal_updated_at),
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
        if exc.promotion_pr_number is not None:
            detail = (
                "a managed promotion PR is open — Blueprint will wait for required "
                "checks, revalidate the exact base/head pair, and retry the merge; "
                "wait for it to land or cancel it before cutting"
            )
        else:
            detail = (
                "a managed promotion is starting — wait for Blueprint to finish "
                "opening the promotion PR, or cancel it before cutting"
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
            "opened_at": utc_iso(new_version.opened_at),
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

    # Scope the verdict lookup to the active attempt so a same-SHA requalification
    # that has started but not yet finalized does not return the prior attempt's
    # findings as though they belonged to the active attempt.  Prior-attempt
    # findings remain in the database but are deliberately invisible until the
    # active attempt produces its own verdict (or the projection lacks durable
    # identity, in which case the newest-row fallback applies).
    verdict = await resolve_newest_qualification_verdict(
        session, open_row, require_audit_findings=True
    )

    if verdict is not None:
        return {
            "findings": get_latest_qualification_audit_findings(open_row, verdict),
            "audit_missing_reason": verdict.audit_missing_reason,
            "finalized_at": utc_iso(verdict.finalized_at),
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


@router.get("/projects/{project_id}/release-versions/{version_number}/summary")
async def get_version_summary(
    project_id: uuid.UUID,
    version_number: int,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return a synthesized 'what's new' summary for a single release version.

    Viewer+ role required.  Keyed by the per-project ``version_number`` that the
    UI already displays.  Returns 404 when no matching version row exists for this
    project.

    Returns the same window-bounded payload as GET /release-versions/{id}/summary
    (the UUID endpoint) — including tasks_merged changelog, structured highlights
    dict, qualification_state, and serving fields — so the CLI fallback path for
    older versions receives the same rich data as the admin UI.

    Backward-compat alias: ``merged_task_count`` mirrors ``merged_count``.
    """
    from app.release_version import (  # noqa: PLC0415
        count_merged_since,
        compute_version_acceptance_aggregate,
        list_merged_tasks_for_version,
    )

    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )

    version = (
        (
            await session.execute(
                select(ReleaseVersion)
                .options(selectinload(ReleaseVersion.deploy_artifacts))
                .where(
                    ReleaseVersion.project_id == project_id,
                    ReleaseVersion.version_number == version_number,
                )
            )
        )
        .scalars()
        .first()
    )
    if version is None:
        raise HTTPException(
            status_code=404,
            detail=f"Release version {version_number} not found for this project.",
        )

    # Bound closed versions to their own span so tasks that merged in a later
    # version are not counted here.  Open versions have no upper bound (until=None).
    version_until = _closed_version_until(version)
    merged_count = await count_merged_since(
        session, project_id, version.opened_at, until=version_until
    )
    tasks_merged = await list_merged_tasks_for_version(
        session, project_id, version.opened_at, until=version_until
    )
    acceptance_aggregate = await compute_version_acceptance_aggregate(
        session, project_id, since=version.opened_at, until=version_until
    )

    aggregate_serving, artifacts = _version_serving_summary(version, project)

    # Route through the shared open-version seam so the returned
    # qualification_state carries the ledger-backed per-dimension sub_score
    # overlay rather than the mutable last_qualification blob, keeping it
    # consistent with the dimension_states other surfaces expose.
    qualification_read = await resolve_open_version_qualification_read_model(
        session, version, project, serving=(aggregate_serving, artifacts)
    )
    qualification_state = qualification_read.qualification_state

    highlights = _release_version.build_version_highlights(
        version,
        merged_count=merged_count,
        aggregate_serving_status=aggregate_serving["serving_status"],
    )

    return {
        # Version identity
        "version_number": version.version_number,
        "phase": version.phase,
        "status": version.status,
        "goal": version.goal,
        # Timestamps (isoformat, null-safe)
        "opened_at": utc_iso(version.opened_at),
        "promoted_at": utc_iso(version.promoted_at),
        "served_at": utc_iso(version.served_at),
        "phase_changed_at": utc_iso(version.phase_changed_at),
        # Merged work
        "merged_count": merged_count,
        "merged_task_count": merged_count,  # backward-compat alias
        "acceptance_aggregate": acceptance_aggregate,
        # Qualification
        "qualification_state": qualification_state,
        "qualification_attempts": version.qualification_attempts,
        "qualification_retry_episode": version.qualification_retry_episode,
        "active_qualification_attempt_id": (
            str(version.active_qualification_attempt_id)
            if version.active_qualification_attempt_id
            else None
        ),
        "qualification_provenance": qualification_read.provenance,
        "dimension_states": qualification_read.dimension_states,
        # Serving fields
        "serving_status": version.serving_status,
        "served_sha": version.served_sha,
        "deploy_failure_reason": version.deploy_failure_reason,
        "aggregate_serving_status": aggregate_serving["serving_status"],
        "artifacts": artifacts,
        "serving": _compose_serving_block(aggregate_serving, artifacts, version),
        # Human-readable summary for display/CLI
        "highlights": highlights,
        # Per-task changelog
        "tasks_merged": tasks_merged,
        # Legacy fields
        "promoted_sha": version.promoted_sha,
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
    ``promoted`` version whose scalar or aggregate serving state is
    ``deploy_failed`` for the project, under a per-version ``with_for_update``
    row lock.

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
    )
    from app.release_serving_observation import (  # noqa: PLC0415
        apply_serving_observation,
        observe_release_artifacts,
        serving_artifact_authority_fingerprint,
        serving_project_authority_fingerprint,
        serving_version_authority_fingerprint,
    )
    from app.time_utils import _utcnow  # noqa: PLC0415

    project_row = await session.get(Project, project_id)
    version = (
        (
            await session.execute(
                select(ReleaseVersion)
                .options(selectinload(ReleaseVersion.deploy_artifacts))
                .where(
                    ReleaseVersion.project_id == project_id,
                    ReleaseVersion.status == "promoted",
                )
                .order_by(ReleaseVersion.version_number.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    aggregate = (
        _version_serving_summary(version, project_row)[0]
        if version is not None
        else None
    )
    if version is None or (
        version.serving_status != "deploy_failed"
        and (aggregate or {}).get("serving_status") != "deploy_failed"
    ):
        raise HTTPException(
            status_code=404,
            detail=(
                "The latest promoted version is not in deploy_failed state for "
                "this project."
            ),
        )

    version_id = version.id
    initial_project_authority = serving_project_authority_fingerprint(project_row)
    initial_version_authority = serving_version_authority_fingerprint(version)
    initial_artifact_authority = serving_artifact_authority_fingerprint(
        list(version.deploy_artifacts)
    )

    # End the read transaction before provider I/O. The exact observation apply
    # will reacquire Project -> Version -> ordered artifact locks and reject any
    # drift from this candidate snapshot.
    await session.rollback()

    now = _utcnow()
    observation = await observe_release_artifacts(
        session,
        version_id,
        now,
        recheck_failed_required=True,
        expected_version_status="promoted",
    )
    if observation is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "The deploy-failed release authority changed while serving was "
                "observed; retry against current state."
            ),
        )
    if (
        observation.project_authority != initial_project_authority
        or observation.version_authority != initial_version_authority
        or observation.artifact_baseline != initial_artifact_authority
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "The selected deploy-failed authority changed before serving "
                "observation began; retry against current state."
            ),
        )
    applied = await apply_serving_observation(session, observation)
    if applied is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "The deploy-failed serving observation was superseded before "
                "it could be applied."
            ),
        )
    version = applied.version
    provider_rows = applied.provider_rows
    backend_artifact = applied.backend_artifact

    latest_promoted_id = (
        await session.execute(
            select(ReleaseVersion.id)
            .where(
                ReleaseVersion.project_id == project_id,
                ReleaseVersion.status == "promoted",
            )
            .order_by(ReleaseVersion.version_number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest_promoted_id != version.id:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "The deploy-failed release was superseded by a newer promotion "
                "while serving was observed."
            ),
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
        clear_mode = "verified"
        attestation_reason = None
        # Capture before expire_all() invalidates the identity-map entry.
        verified_served_identifier = backend_artifact.served_identifier
        verified_served_at = backend_artifact.served_at
    else:
        # Force override is ONLY valid when the failure reason is
        # serving_sha_unobservable: a revision went STABLE but the live SHA
        # could not be read, so the deploy may be genuinely live.  For all
        # other reasons (sha_mismatch, rolled_back, halted, no_new_revision)
        # the deploy is known to have failed and cannot be force-attested.
        #
        # Use backend_artifact.reason_code (freshly rechecked by
        # _refresh_release_artifact_rows) rather than the stale aggregate scalar
        # version.deploy_failure_reason, which may not reflect a backend reason
        # that transitioned after the latch.  Fall back to the scalar only when no
        # backend artifact row exists.
        effective_failure_reason = (
            backend_artifact.reason_code
            if backend_artifact is not None
            else version.deploy_failure_reason
        )
        if effective_failure_reason != "serving_sha_unobservable":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"force override is not permitted for deploy_failure_reason "
                    f"'{effective_failure_reason}': the force path only "
                    "applies when the serving SHA is unobservable "
                    "(serving_sha_unobservable). A confirmed deployment failure "
                    "cannot be force-cleared."
                ),
            )
        # Live serving SHA is unobservable — refuse unless the operator
        # explicitly overrides AND records a reason.
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
        clear_mode = "operator_attested"

    authorship_plan = None
    if clear_mode == "verified":
        # Independently confirmed serving — clear with the OBSERVED served SHA.
        # Use pre-captured locals: backend_artifact was expired by expire_all().
        authorship_plan = await _recover_serving_confirmed(
            session,
            version,
            verified_served_identifier,
            verified_served_at,
            now,
        )
    else:
        # Operator attestation: clear using the promoted SHA as the served SHA and
        # record the provenance in the durable audit trail.
        authorship_plan = await _recover_serving_confirmed(
            session, version, version.promoted_sha, now, now
        )
        _record_deploy_failed_clear_attestation(
            version, operator_id, attestation_reason, now
        )

    await session.commit()
    if authorship_plan is not None:
        from app.release_commit_authorship import measure_serving_authorship_evidence

        await measure_serving_authorship_evidence(
            version.id,
            expected_receipt_identity=authorship_plan["receipt_identity"],
        )
    await session.refresh(version)
    return {
        "version_number": version.version_number,
        "serving_status": version.serving_status,
        "served_sha": version.served_sha,
        "served_at": utc_iso(version.served_at),
        "deploy_failure_reason": version.deploy_failure_reason,
        "clear_mode": clear_mode,
        "operator_attested": clear_mode == "operator_attested",
        "attestation_reason": attestation_reason,
    }


@router.post("/projects/{project_id}/approve-activation")
async def approve_activation(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Approve activation of the latest promoted, still-unconfirmed version.

    Admin-gated (matching the deploy operator levers).  Targets the latest
    ``promoted`` version under a ``with_for_update`` row lock, with no request
    body: the approval always aims at the current promoted version, so it can
    never be aimed at a different or historical artifact.  The approval binds
    EXACTLY this version's immutable ``promoted_sha`` — the activation-receipt
    path refuses any reported revision that differs, or a receipt once a newer
    promotion supersedes this version.

    Requires a promoted version whose aggregate serving status is either
    ``unconfirmed`` or ``confirmation_timed_out``.  The latter is a
    non-terminal report: its confirmation window elapsed without establishing
    serving, so the same explicit operator approval remains available to
    re-arm activation.  Already ``serving_confirmed`` / ``deploy_failed`` (or a
    never-armed ``serving_status`` NULL) -> 409.
    Idempotent: re-approving a still-unconfirmed version returns the existing
    approval without re-stamping.

    The gates, the target selection and the durable provenance live in
    ``app.release_activation_approval`` so this human path and the Resident's
    ``approve_activation`` strategic op cannot drift apart.  This endpoint's
    only distinct responsibility is establishing WHO is approving.
    """
    operator_id = current_user.get("email") or current_user["user_id"]
    return await approve_latest_promoted_activation(
        session,
        project_id,
        ActivationApprover.human(operator_id),
    )


_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


class _ActivationReceiptBody(BaseModel):
    # The promoted version_number this activation bound to.
    version_number: int
    # The activated revision (40-hex SHA). Defaults to the version's promoted_sha.
    revision: str | None = _Field(default=None, max_length=40)
    # SSH host that performed the activation (provenance, informational).
    host: str | None = _Field(default=None, max_length=200)
@router.post("/projects/{project_id}/release-versions/{version_number}/prepromotion-rehearsal")
async def request_prepromotion_rehearsal(
    project_id: uuid.UUID,
    version_number: int,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Queue trusted-main CI build/smoke for the exact open candidate.

    This writes the same durable outbox intent as automatic qualification. The
    reaper dispatches the trusted-main workflow after the transaction commits;
    the request never performs GitHub I/O while holding the release row lock.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="owner"
    )
    if await lock_release_project(session, project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    version = (
        await session.execute(
            select(ReleaseVersion)
            .where(
                ReleaseVersion.project_id == project_id,
                ReleaseVersion.version_number == version_number,
            )
            .with_for_update()
        )
    ).scalars().first()
    if version is None:
        raise HTTPException(status_code=404, detail="Release version not found.")
    if version.status != "open":
        raise HTTPException(
            status_code=409,
            detail=f"Release version {version_number} is not an open candidate.",
        )
    candidate_sha = str((version.last_qualification or {}).get("candidate_sha") or "")
    if not _SHA40_RE.fullmatch(candidate_sha):
        raise HTTPException(
            status_code=409,
            detail="The open release version has no full candidate SHA to rehearse.",
        )

    from app.release_prepromotion_rehearsal import (
        prepare_rehearsal_dispatch_intent,
    )

    ready, dispatch = await prepare_rehearsal_dispatch_intent(
        session, version, candidate_sha, rearm_terminal=True
    )
    await session.commit()
    return {
        "version_number": version_number,
        "candidate_sha": candidate_sha,
        "dispatch": dispatch,
        "status": "ready" if ready else "rehearsal_dispatch_pending",
    }


@router.post("/projects/{project_id}/activation-receipt")
async def record_activation_receipt(
    project_id: uuid.UUID,
    payload: _ActivationReceiptBody,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Record an operator activation of a promoted version and close the loop.

    The one-command activation path (``blueprint deploy``) binds the built
    revision to the project's latest promoted version, activates it on the
    control-plane host, then POSTs this receipt.  The backend stamps the receipt
    provenance on the version's ``last_qualification``, confirms the
    ``backend_revision`` artifact with the served identifier, and — when every
    required artifact now confirms — resolves the version to
    ``serving_confirmed`` through the shared recovery machinery.

    Idempotent: re-POSTing an identical receipt for an already
    ``serving_confirmed`` version with a matching ``served_sha`` returns the
    current state without error.
    """
    from app.release_reconciliation import (  # noqa: PLC0415
        _confirm_serving_or_fail,
        _record_activation_receipt,
    )
    from app.release_activation_receipt import (  # noqa: PLC0415
        replay_activation_receipt,
    )
    from app.release_serving_observation import (  # noqa: PLC0415
        serving_project_authority_fingerprint,
        serving_version_authority_fingerprint,
    )
    from app.time_utils import _utcnow  # noqa: PLC0415

    if payload.revision is not None and not _SHA40_RE.fullmatch(payload.revision):
        raise HTTPException(
            status_code=400,
            detail="revision must be a 40-character lowercase hex SHA.",
        )

    version = (
        (
            await session.execute(
                select(ReleaseVersion)
                .where(
                    ReleaseVersion.project_id == project_id,
                    ReleaseVersion.version_number == payload.version_number,
                )
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if version is None:
        raise HTTPException(status_code=404, detail="Release version not found.")
    has_receipt = "activation_receipt" in (version.last_qualification or {})
    if not has_receipt and version.status != "promoted":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Release version {version.version_number} is not promoted "
                f"(status={version.status}). Only promoted versions can be "
                "confirmed serving by an activation receipt."
            ),
        )

    effective_sha = (payload.revision or version.promoted_sha or "").strip()
    if not _SHA40_RE.fullmatch(effective_sha):
        raise HTTPException(
            status_code=409,
            detail="The promoted version has no valid revision to receipt.",
        )
    project = await session.get(Project, project_id, populate_existing=True)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    observed_project_authority = serving_project_authority_fingerprint(project)
    observed_version_authority = serving_version_authority_fingerprint(version)

    # Approved-activation guard: an in-product approval authorizes EXACTLY the
    # approved version's promoted SHA, so a receipt under that approval must
    # never confirm a different or historical artifact.  Applied whenever an
    # approval exists and the version is not yet resolved (including a
    # never-armed promotion with serving_status NULL); the legacy CLI receipt
    # path (no approval) and already-resolved versions are unchanged.
    if not has_receipt and version.activation_requested_at is not None:
        from app.release_activation import (  # noqa: PLC0415
            reject_stale_or_mismatched_approved_receipt,
        )

        rejection_detail = await reject_stale_or_mismatched_approved_receipt(
            session, version, effective_sha
        )
        if rejection_detail:
            raise HTTPException(status_code=409, detail=rejection_detail)

    operator_id = str(current_user["user_id"])
    operator_display = str(current_user.get("email") or operator_id)
    now = _utcnow()
    version_id = version.id

    # The approval/ancestry preflight above may perform GitHub I/O. End its
    # read transaction before taking release-authority locks, then prove that
    # the exact project/version authority observed by that preflight is still
    # current inside the bounded receipt transaction.
    await session.rollback()
    locked_project = await lock_release_project(session, project_id)
    if locked_project is None:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "Activation project authority changed during verification; "
                "retry against current state."
            ),
        )
    version = (
        (
            await session.execute(
                select(ReleaseVersion)
                .where(
                    ReleaseVersion.id == version_id,
                    ReleaseVersion.project_id == project_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    try:
        replay = (
            None
            if version is None
            else replay_activation_receipt(
                version,
                revision=payload.revision,
                host=payload.host,
                operator_id=operator_id,
                operator_display=operator_display,
            )
        )
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if replay is not None:
        replay_version_id = version.id
        await session.rollback()
        await _confirm_serving_or_fail(replay_version_id)
        session.expire_all()
        version = await session.get(
            ReleaseVersion, replay_version_id, populate_existing=True
        )
        if version is None:
            raise HTTPException(status_code=404, detail="Release version not found.")
        replay["serving_status"] = version.serving_status or replay["serving_status"]
        replay["served_sha"] = version.served_sha or replay["served_sha"]
        return {
            "version_number": version.version_number,
            "serving_status": version.serving_status,
            "served_sha": version.served_sha,
            "served_at": utc_iso(version.served_at),
            "deploy_failure_reason": version.deploy_failure_reason,
            "receipt": replay,
        }
    if (
        version is None
        or serving_project_authority_fingerprint(locked_project)
        != observed_project_authority
        or serving_version_authority_fingerprint(version)
        != observed_version_authority
    ):
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "Activation version authority was superseded before the receipt "
                "could be recorded."
            ),
        )

    latest_promoted_id = (
        await session.execute(
            select(ReleaseVersion.id)
            .where(
                ReleaseVersion.project_id == project_id,
                ReleaseVersion.status == "promoted",
            )
            .order_by(ReleaseVersion.version_number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest_promoted_id != version.id:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Activation receipt was superseded by a newer promoted version.",
        )

    # Serialize all existing artifact rows before the local receipt projection.
    # The helper may seed a newly registered artifact, but never calls a
    # provider. Project->Version ordering makes that create race-safe.
    from app.models import ReleaseDeployArtifact  # noqa: PLC0415

    await session.execute(
        select(ReleaseDeployArtifact)
        .where(ReleaseDeployArtifact.version_id == version.id)
        .order_by(
            ReleaseDeployArtifact.artifact_kind,
            ReleaseDeployArtifact.id,
        )
        .with_for_update()
    )
    try:
        receipt = await _record_activation_receipt(
            session,
            version,
            revision=payload.revision,
            host=payload.host,
            operator_id=operator_id,
            operator_display=operator_display,
            now=now,
        )
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    authorship_plan = receipt.pop("_authorship_plan", None)
    await session.commit()
    if authorship_plan is not None:
        from app.release_commit_authorship import measure_serving_authorship_evidence

        await measure_serving_authorship_evidence(
            version.id,
            expected_receipt_identity=authorship_plan["receipt_identity"],
        )

    # The host-side effect and its immutable receipt are now durable. Provider
    # observation is an ordinary, independently retryable continuation and can
    # fail without losing or ambiguating the activation receipt.
    await _confirm_serving_or_fail(version_id)
    session.expire_all()
    version = await session.get(ReleaseVersion, version_id, populate_existing=True)
    if version is None:
        raise HTTPException(status_code=404, detail="Release version not found.")
    receipt["serving_status"] = version.serving_status or receipt["serving_status"]
    receipt["served_sha"] = version.served_sha or effective_sha
    return {
        "version_number": version.version_number,
        "serving_status": version.serving_status,
        "served_sha": version.served_sha,
        "served_at": utc_iso(version.served_at),
        "deploy_failure_reason": version.deploy_failure_reason,
        "receipt": receipt,
    }


@router.get("/release-versions/{version_id}/summary")
async def get_release_version_summary(
    version_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return a structured 'what's new' summary for a single release version.

    Works for any version status (open, promoted, or abandoned). Synthesizes
    merged-work count, acceptance quality, qualification state, and a human-
    readable headline into a single response — no need to stitch several
    existing endpoints together.

    Access is enforced per the owning project (viewer role or above).
    """
    from app.release_version import (  # noqa: PLC0415
        count_merged_since,
        compute_version_acceptance_aggregate,
        list_merged_tasks_for_version,
    )

    version = (
        await session.execute(
            select(ReleaseVersion)
            .options(selectinload(ReleaseVersion.deploy_artifacts))
            .where(ReleaseVersion.id == version_id)
        )
    ).scalar_one_or_none()

    if version is None:
        raise HTTPException(status_code=404, detail="Release version not found.")

    project = await _get_user_project_with_role(
        version.project_id, current_user["user_id"], session, min_role="viewer"
    )

    # Bound closed versions to their own span so tasks that merged in a later
    # version are not counted here.  Open versions have no upper bound (until=None).
    version_until = _closed_version_until(version)
    merged_count = await count_merged_since(
        session, version.project_id, version.opened_at, until=version_until
    )
    tasks_merged = await list_merged_tasks_for_version(
        session, version.project_id, version.opened_at, until=version_until
    )
    acceptance_aggregate = await compute_version_acceptance_aggregate(
        session, version.project_id, since=version.opened_at, until=version_until
    )

    aggregate_serving, artifacts = _version_serving_summary(version, project)

    # Route through the shared open-version seam so the returned
    # qualification_state carries the ledger-backed per-dimension sub_score
    # overlay rather than the mutable last_qualification blob, keeping it
    # consistent with the dimension_states other surfaces expose.
    qualification_read = await resolve_open_version_qualification_read_model(
        session, version, project, serving=(aggregate_serving, artifacts)
    )
    qualification_state = qualification_read.qualification_state

    # Derive highlights from the data above — no extra I/O. Shared helper so the
    # version-lifecycle current_version block renders an identical headline; the
    # 'serving' determination is driven off the aggregate serving status, not the
    # scalar serving_status column.
    highlights = _release_version.build_version_highlights(
        version,
        merged_count=merged_count,
        aggregate_serving_status=aggregate_serving["serving_status"],
    )

    return {
        # Version identity
        "version_number": version.version_number,
        "phase": version.phase,
        "status": version.status,
        "goal": version.goal,
        # Timestamps (isoformat, null-safe)
        "opened_at": utc_iso(version.opened_at),
        "promoted_at": utc_iso(version.promoted_at),
        "served_at": utc_iso(version.served_at),
        "phase_changed_at": utc_iso(version.phase_changed_at),
        # Merged work
        "merged_count": merged_count,
        "acceptance_aggregate": acceptance_aggregate,
        # Qualification state
        "qualification_state": qualification_state,
        "qualification_attempts": version.qualification_attempts,
        "qualification_retry_episode": version.qualification_retry_episode,
        "active_qualification_attempt_id": (
            str(version.active_qualification_attempt_id)
            if version.active_qualification_attempt_id
            else None
        ),
        "qualification_provenance": qualification_read.provenance,
        "dimension_states": qualification_read.dimension_states,
        # Serving fields
        "serving_status": version.serving_status,
        "served_sha": version.served_sha,
        "deploy_failure_reason": version.deploy_failure_reason,
        "aggregate_serving_status": aggregate_serving["serving_status"],
        "artifacts": artifacts,
        "serving": _compose_serving_block(aggregate_serving, artifacts, version),
        # Human-readable summary for display/CLI
        "highlights": highlights,
        # Per-task changelog: type, title, and PR number for each merged task,
        # newest-first. Allows operators and the CLI to see what shipped.
        "tasks_merged": tasks_merged,
    }


_RELEASE_VERSION_STATUSES = {"open", "promoted", "abandoned"}


@router.get("/projects/{project_id}/release-versions")
async def list_release_versions(
    project_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: Optional[str] = Query(default=None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """List all release versions for a project, newest-first, paginated.

    Unlike ``version-lifecycle`` (which surfaces only the open version plus a
    short recent tail and is cached for dashboard polling), this is an explicit
    operator query that browses the FULL version history. Each entry is the same
    rich serialization the lifecycle endpoint's history section emits (merged
    counts, highlights, serving state).

    Query params:
    - ``limit`` (1-100, default 20): page size.
    - ``offset`` (>=0, default 0): rows to skip.
    - ``status`` (optional ``open`` | ``promoted`` | ``abandoned``): filter.

    Returns ``{"versions": [...], "total": N, "limit": N, "offset": N}`` where
    ``total`` is the count of matching rows BEFORE pagination. Viewer role or
    above on the owning project is required. Not cached - this is an explicit
    operator query, not dashboard polling.
    """
    if status is not None and status not in _RELEASE_VERSION_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                "status must be one of: " + ", ".join(sorted(_RELEASE_VERSION_STATUSES))
            ),
        )

    project_row = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="viewer"
    )

    filters = [ReleaseVersion.project_id == project_id]
    if status is not None:
        filters.append(ReleaseVersion.status == status)

    total = (
        await session.execute(
            select(func.count()).select_from(ReleaseVersion).where(*filters)
        )
    ).scalar_one()

    rows = (
        (
            await session.execute(
                select(ReleaseVersion)
                .options(selectinload(ReleaseVersion.deploy_artifacts))
                .where(*filters)
                .order_by(ReleaseVersion.opened_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    versions = [
        await _serialize_version_history_entry(
            version,
            project_row,
            session=session,
            project_id=project_id,
        )
        for version in rows
    ]

    return {
        "versions": versions,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
