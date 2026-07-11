"""Release version lifecycle endpoints."""

import asyncio
import logging
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
from app.release_artifacts import version_serving_summary as _version_serving_summary
from app.qualification_state_resolver import (
    resolve_effective_qualification_state,
    resolve_open_version_qualification_state,
    resolve_promotion_blocking_result,
    resolve_dimension_states,
    resolve_dimension_states_from_ledger,
    patch_qualification_state_from_dimension_states,
)
from app.time_utils import _coerce_naive_utc
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
        version.served_at.isoformat() if version.served_at else None,
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
    from app.release_readiness import format_qualification_for_api  # noqa: PLC0415

    out = format_qualification_for_api(qual)
    out.update(
        {
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
        "opened_at": version.opened_at.isoformat(),
        "promoted_at": version.promoted_at.isoformat() if version.promoted_at else None,
        "qualification_attempts": version.qualification_attempts,
        "last_qualification": _format_qualification(version.last_qualification)
        if version.last_qualification
        else None,
        # Post-promotion serving confirmation — disambiguates a merged
        # promotion from one that actually shipped (additive fields).
        "serving_status": version.serving_status,
        "served_sha": version.served_sha,
        "served_at": version.served_at.isoformat() if version.served_at else None,
        "deploy_failure_reason": version.deploy_failure_reason,
        "deploy_confirm_deadline": version.deploy_confirm_deadline.isoformat()
        if version.deploy_confirm_deadline
        else None,
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

    The response is cached per project for 30 seconds (in-flight dedup + stale
    fallback) to bound the DB work from dashboard polling at ~30s cadence.
    ``precomputed`` in the response is ``true`` when a cached value was served.
    """
    from app.release_version import (  # noqa: PLC0415
        count_merged_since,
        count_in_flight_tasks,
        list_merged_tasks_for_version,
    )
    from app.release_qualifier import (  # noqa: PLC0415
        compute_promotion_readiness,
        resolve_lifecycle_audit_evidence,
        _resolve_policy_with_provenance,
    )
    from app.models import Project  # noqa: PLC0415
    from app.strategic_spawning import (  # noqa: PLC0415
        _build_staged_directions_summary,
    )

    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="viewer"
    )

    async def _compute_lifecycle() -> Dict[str, Any]:
        project_row = (
            await session.execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        _, promotion_policy_source, promotion_policy_error = (
            _resolve_policy_with_provenance()
        )

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
                from app.acceptance_criteria import (  # noqa: PLC0415
                    get_version_coverage_summary as _get_coverage,
                )
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
                _cov_raw = await _get_coverage(session, open_row.id)
                _cov = {
                    "total": int(_cov_raw.get("total") or 0),
                    "covered": int(_cov_raw.get("covered") or 0),
                }
                _rdns = await _compute_readiness(session, project_id)
                _qualification_trigger_state = _explain_trigger(
                    phase=open_row.phase or "",
                    status=_lq_status,
                    error_code=_lq.get("error") if _lq_status == "error" else None,
                    qualification_attempts=open_row.qualification_attempts or 0,
                    coverage=_cov,
                    requalification_ready=_rdns.get("requalification_ready"),
                    candidate_sha=_lq.get("candidate_sha"),
                    automain_sha=None,  # not fetched in the read path
                    automain_ci_state=None,
                    blocker=_lq.get("blocker"),
                    in_flight=_lq_status == "pending",
                    promotion_in_flight=bool(_lq.get("promotion_pending")),
                    last_attempt=_lq if _lq else None,
                    knob=_get_trigger_mode(),
                    evaluated_at=_utcnow().isoformat(),
                )
            except Exception:
                logger.exception(
                    "[VERSION_LIFECYCLE] qualification_trigger_state compute failed "
                    "for project %s; serving lifecycle without the trigger signal",
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
            qual_state, _ = await resolve_open_version_qualification_state(
                session,
                open_row,
                project_row,
                audit_child=latest_audit,
                serving=(aggregate_serving, artifact_evidence),
            )
            # Hoist dimension_states so we can patch qual_state sub_scores from
            # the ledger before assembling the response — one consistent story.
            _open_dim_states = await resolve_dimension_states_from_ledger(session, open_row)
            qual_state = patch_qualification_state_from_dimension_states(
                qual_state, _open_dim_states
            )

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
                "served_at": open_row.served_at.isoformat()
                if open_row.served_at
                else None,
                "deploy_failure_reason": open_row.deploy_failure_reason,
                "aggregate_serving_status": aggregate_serving["serving_status"],
                "aggregate_deploy_failure_reason": aggregate_serving["reason_code"],
                "artifacts": artifact_evidence,
                "serving": _compose_serving_block(
                    aggregate_serving, artifact_evidence, open_row
                ),
                "deploy_confirm_deadline": open_row.deploy_confirm_deadline.isoformat()
                if open_row.deploy_confirm_deadline
                else None,
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
            "promotion_policy_source": promotion_policy_source,
            "promotion_policy_error": promotion_policy_error,
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
    return {**result, "precomputed": served_stale}


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
    # Re-check against the LOCKED row with finalized-verdict substitution so a
    # stale-pending+finalized-verdict is not misclassified as still-blocking.
    _promo_state = await resolve_promotion_blocking_result(session, open_ver)
    if _promo_state is not None:
        if _promo_state.get("status") == "promotion_check_wait":
            detail = (
                "a migration-carrying promotion PR is open and waiting for "
                "required checks — Blueprint will retry the merge automatically; "
                "wait for it to land or cancel it before changing the phase"
            )
        else:
            detail = (
                "a promotion PR is open and auto-merging — wait for it to land or "
                "cancel it before changing the phase"
            )
        pr_number = _promo_state.get("promotion_pr_number")
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
    _release_version.set_version_goal(open_ver, payload.goal, editor_id=operator_id)
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
        _cut_state = (exc.promotion_freshness or {}).get("state")
        if _cut_state == "promotion_check_wait":
            detail = (
                "a migration-carrying promotion PR is open and waiting for "
                "required checks — Blueprint will retry the merge automatically; "
                "wait for it to land or cancel it before cutting"
            )
        else:
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

    qualification_state, _ = resolve_effective_qualification_state(
        version, project, serving=(aggregate_serving, artifacts)
    )

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
        "opened_at": version.opened_at.isoformat() if version.opened_at else None,
        "promoted_at": version.promoted_at.isoformat() if version.promoted_at else None,
        "served_at": version.served_at.isoformat() if version.served_at else None,
        "phase_changed_at": version.phase_changed_at.isoformat()
        if version.phase_changed_at
        else None,
        # Merged work
        "merged_count": merged_count,
        "merged_task_count": merged_count,  # backward-compat alias
        "acceptance_aggregate": acceptance_aggregate,
        # Qualification
        "qualification_state": qualification_state,
        "qualification_attempts": version.qualification_attempts,
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
        _refresh_release_artifact_rows,
    )
    from app.time_utils import _utcnow  # noqa: PLC0415

    version = None
    project_row = await session.get(Project, project_id)
    promoted_versions = (
        (
            await session.execute(
                select(ReleaseVersion)
                .options(selectinload(ReleaseVersion.deploy_artifacts))
                .where(
                    ReleaseVersion.project_id == project_id,
                    ReleaseVersion.status == "promoted",
                )
                .order_by(ReleaseVersion.promoted_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    for candidate in promoted_versions:
        aggregate, _ = _version_serving_summary(candidate, project_row)
        if (
            candidate.serving_status == "deploy_failed"
            or aggregate["serving_status"] == "deploy_failed"
        ):
            await session.execute(
                select(ReleaseVersion)
                .where(ReleaseVersion.id == candidate.id)
                .with_for_update()
            )
            version = candidate
            break
    if version is None:
        raise HTTPException(
            status_code=404,
            detail="No promoted version in deploy_failed state for this project.",
        )

    version_id = version.id

    # Release the row lock before network I/O, following the established reconcile
    # pattern in release_reconciliation.py.  The session uses expire_on_commit=False
    # so version attrs remain accessible after the commit.
    await session.commit()

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

    # Re-acquire the row lock for the write phase.  All validation is complete;
    # _recover_serving_confirmed requires an already-locked session.
    # Expire the identity-map copy before re-locking so we evaluate current DB
    # state, not the cached pre-I/O instance (matches reconcile pattern in
    # release_reconciliation.py lines 208-224).
    session.expire_all()
    version = (
        (
            await session.execute(
                select(ReleaseVersion)
                .where(ReleaseVersion.id == version_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )

    if version is None:
        raise HTTPException(status_code=404, detail="Release version not found.")

    if clear_mode == "verified":
        # Independently confirmed serving — clear with the OBSERVED served SHA.
        # Use pre-captured locals: backend_artifact was expired by expire_all().
        await _recover_serving_confirmed(
            session,
            version,
            verified_served_identifier,
            verified_served_at,
            now,
        )
    else:
        # Operator attestation: clear using the promoted SHA as the served SHA and
        # record the provenance in the durable audit trail.
        await _recover_serving_confirmed(
            session, version, version.promoted_sha, now, now
        )
        _record_deploy_failed_clear_attestation(
            version, operator_id, attestation_reason, now
        )

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

    qualification_state, _ = resolve_effective_qualification_state(
        version, project, serving=(aggregate_serving, artifacts)
    )

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
        "opened_at": version.opened_at.isoformat() if version.opened_at else None,
        "promoted_at": version.promoted_at.isoformat() if version.promoted_at else None,
        "served_at": version.served_at.isoformat() if version.served_at else None,
        "phase_changed_at": version.phase_changed_at.isoformat()
        if version.phase_changed_at
        else None,
        # Merged work
        "merged_count": merged_count,
        "acceptance_aggregate": acceptance_aggregate,
        # Qualification state
        "qualification_state": qualification_state,
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
