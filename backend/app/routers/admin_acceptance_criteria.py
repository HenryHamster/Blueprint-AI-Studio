"""Admin endpoints for version-scoped acceptance criteria management.

Provides:
  GET    /api/admin/projects/{project_id}/acceptance-criteria                          — list criteria for open version
  POST   /api/admin/projects/{project_id}/acceptance-criteria                          — create a criterion
  DELETE /api/admin/projects/{project_id}/acceptance-criteria/{key}                    — delete a criterion
  POST   /api/admin/projects/{project_id}/acceptance-criteria/link-direction           — link criteria keys to a direction
  POST   /api/admin/projects/{project_id}/acceptance-criteria/{criterion_key}/attest   — operator attestation waiver
"""

import logging
import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.time_utils import _utcnow, utc_iso

from app import acceptance_criteria as ac
from app.admin import _get_user_project_with_role
from app.auth import get_current_user
from app.database import get_session
from app.models import CriterionEvidence, Direction

router = APIRouter(prefix="/api/admin")
logger = logging.getLogger(__name__)


def _is_machine_contract(raw: object) -> bool:
    return ac.normalize_evidence_contract(raw) is not None


@router.get("/projects/{project_id}/acceptance-criteria")
async def list_acceptance_criteria(
    project_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return criteria and coverage summary for the project's open release version."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )
    from app.release_version import current_open_version

    open_version = await current_open_version(session, project_id)
    if open_version is None:
        return {"release_version_id": None, "criteria": [], "total": 0}
    summary = await ac.get_version_coverage_summary(session, open_version.id)
    return {
        "release_version_id": str(open_version.id),
        "version_number": open_version.version_number,
        **summary,
    }


class CreateCriterionRequest(BaseModel):
    text: str
    evidence_contract: dict | None = None


class UpdateEvidenceContractRequest(BaseModel):
    evidence_contract: dict | None = None


@router.post("/projects/{project_id}/acceptance-criteria")
async def create_acceptance_criterion(
    project_id: uuid.UUID,
    body: CreateCriterionRequest,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create a new acceptance criterion for the project's open release version.

    Enforces a per-version cap of MAX_CRITERIA_PER_VERSION criteria.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    if body.evidence_contract is not None:
        # Machine evidence can trigger external work and deterministically
        # override a release verdict. Keep ordinary text criteria member-
        # writable, but reserve machine-policy activation to project owners.
        await _get_user_project_with_role(
            project_id, current_user["user_id"], session, "owner"
        )
    from app.release_version import current_open_version

    open_version = await current_open_version(session, project_id)
    if open_version is None:
        raise HTTPException(status_code=409, detail="No open release version")

    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="text must not be empty")

    existing_count = await ac.count_active_criteria_for_version(session, open_version.id)
    if existing_count >= ac.MAX_CRITERIA_PER_VERSION:
        raise HTTPException(
            status_code=409,
            detail=f"Version already has {ac.MAX_CRITERIA_PER_VERSION} criteria (cap reached)",
        )

    try:
        criterion = await ac.create_criterion(
            session,
            project_id=project_id,
            release_version_id=open_version.id,
            text=text,
            evidence_contract=body.evidence_contract,
            created_by="operator",
            audit_actor=str(current_user["user_id"]),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    logger.info(
        "[AC] Operator created criterion %s for project %s version %s",
        criterion.criterion_key,
        project_id,
        open_version.id,
    )
    return {
        "criterion_key": criterion.criterion_key,
        "text": criterion.text,
        "evidence_contract": criterion.evidence_contract,
        "evidence_contract_updated_at": utc_iso(criterion.evidence_contract_updated_at),
        "evidence_contract_updated_by": criterion.evidence_contract_updated_by,
        "release_version_id": str(open_version.id),
    }


@router.patch(
    "/projects/{project_id}/acceptance-criteria/{criterion_key}"
)
@router.patch(
    "/projects/{project_id}/acceptance-criteria/{criterion_key}/evidence-contract"
)
async def update_acceptance_criterion_evidence_contract(
    project_id: uuid.UUID,
    criterion_key: str,
    body: UpdateEvidenceContractRequest,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Set or clear an existing criterion's canonical machine contract."""
    # This route only changes machine release policy (including clearing it),
    # so every invocation requires the project owner.
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    from app.release_version import current_open_version

    open_version = await current_open_version(session, project_id)
    if open_version is None:
        raise HTTPException(status_code=409, detail="No open release version")
    try:
        criterion = await ac.update_criterion_evidence_contract(
            session,
            project_id=project_id,
            release_version_id=open_version.id,
            criterion_key=criterion_key,
            evidence_contract=body.evidence_contract,
            updated_by=str(current_user["user_id"]),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if criterion is None:
        raise HTTPException(status_code=404, detail="Criterion not found")
    await session.commit()
    return {
        "status": "ok",
        "criterion_key": criterion.criterion_key,
        "text": criterion.text,
        "evidence_contract": criterion.evidence_contract,
        "evidence_contract_updated_at": utc_iso(criterion.evidence_contract_updated_at),
        "evidence_contract_updated_by": criterion.evidence_contract_updated_by,
        "release_version_id": str(open_version.id),
    }


@router.delete("/projects/{project_id}/acceptance-criteria/{criterion_key}")
async def delete_acceptance_criterion(
    project_id: uuid.UUID,
    criterion_key: str,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Delete a criterion from the open release version by its criterion_key."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    from app.release_version import current_open_version

    open_version = await current_open_version(session, project_id)
    if open_version is None:
        raise HTTPException(status_code=409, detail="No open release version")

    existing = await ac.get_criterion_by_key_for_update(
        session,
        project_id,
        open_version.id,
        criterion_key,
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="Criterion not found")
    if _is_machine_contract(existing.evidence_contract):
        await _get_user_project_with_role(
            project_id, current_user["user_id"], session, "owner"
        )

    deleted = await ac.delete_criterion(
        session,
        project_id=project_id,
        release_version_id=open_version.id,
        criterion_key=criterion_key,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Criterion not found")

    logger.info(
        "[AC] Operator deleted criterion %s from project %s version %s",
        criterion_key,
        project_id,
        open_version.id,
    )
    return {"status": "deleted", "criterion_key": criterion_key}


@router.post("/projects/{project_id}/acceptance-criteria/{criterion_key}/reinstate")
async def reinstate_acceptance_criterion(
    project_id: uuid.UUID,
    criterion_key: str,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Reinstate a retracted or superseded criterion back to active status.

    This operator-only endpoint reverses a loop-driven retraction the operator
    disagrees with. Idempotent: calling on an already-active criterion returns 200.
    Enforces the per-version active-criteria cap before reinstating.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    from app.release_version import current_open_version

    open_version = await current_open_version(session, project_id)
    if open_version is None:
        raise HTTPException(status_code=409, detail="No open release version")

    row = await ac.get_criterion_by_key_for_update(
        session,
        project_id,
        open_version.id,
        criterion_key,
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Criterion key not found in open version: {criterion_key}",
        )

    if _is_machine_contract(row.evidence_contract):
        await _get_user_project_with_role(
            project_id, current_user["user_id"], session, "owner"
        )
    if row.status != "active":
        active_count = await ac.count_active_criteria_for_version(session, open_version.id)
        if active_count >= ac.MAX_CRITERIA_PER_VERSION:
            raise HTTPException(
                status_code=409,
                detail=f"Version already has {ac.MAX_CRITERIA_PER_VERSION} active criteria (cap reached); cannot reinstate",
            )

    reinstated = await ac.reinstate_criterion(
        session,
        project_id=project_id,
        release_version_id=open_version.id,
        criterion_key=criterion_key,
    )
    if reinstated is None:
        raise HTTPException(status_code=404, detail="Criterion not found")

    logger.info(
        "[AC] Operator reinstated criterion %s for project %s version %s",
        criterion_key,
        project_id,
        open_version.id,
    )
    return {"status": "ok", "criterion_key": criterion_key}


class LinkDirectionRequest(BaseModel):
    direction_key: str = Field(..., max_length=128)
    criterion_keys: List[str] = Field(..., max_length=50)


@router.post("/projects/{project_id}/acceptance-criteria/link-direction")
async def link_criteria_to_direction(
    project_id: uuid.UUID,
    body: LinkDirectionRequest,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Link a set of criterion keys to a direction row (sets Direction.criterion_keys).

    Validates that every supplied criterion_key exists in the current open
    release version.  Keys from closed or older versions are rejected.
    Duplicate keys in the request are silently de-duplicated.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    from app.release_version import current_open_version

    open_version = await current_open_version(session, project_id)
    if open_version is None:
        raise HTTPException(status_code=409, detail="No open release version")

    # De-duplicate while preserving order.
    deduped_keys = list(dict.fromkeys(body.criterion_keys))

    # Validate all supplied keys against the version's active/inactive sets in
    # one query (guard mirrors check_gap_id_write in problem_model.py).
    if deduped_keys:
        from app.direction_referential_guard import (
            check_criterion_key_write,
            load_version_criterion_key_sets,
        )

        active_keys, inactive_keys = await load_version_criterion_key_sets(
            session, project_id, open_version.id
        )
        failures: dict[str, list[str]] = {}
        for k in deduped_keys:
            cls, _ = check_criterion_key_write(k, active_keys, inactive_keys)
            if cls is not None:
                failures.setdefault(cls, []).append(k)
        if failures:
            # Prioritize dangling over inactive in the error report.
            classification = "dangling" if "dangling" in failures else "inactive"
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "criterion_key_invalid",
                    "classification": classification,
                    "keys": failures[classification],
                    "active_criterion_keys": sorted(active_keys),
                },
            )

    row = (
        await session.execute(
            select(Direction).where(
                Direction.project_id == project_id,
                Direction.direction_key == body.direction_key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Direction not found")

    row.criterion_keys = deduped_keys
    row_data = dict(row.data or {})
    if deduped_keys:
        row_data[ac.CRITERION_RELEASE_VERSION_CONTEXT_KEY] = str(open_version.id)
    else:
        row_data.pop(ac.CRITERION_RELEASE_VERSION_CONTEXT_KEY, None)
    row.data = row_data
    session.add(row)
    await session.commit()
    logger.info(
        "[AC] Linked %d criterion key(s) to direction %s for project %s",
        len(deduped_keys),
        body.direction_key,
        project_id,
    )

    # Best-effort backfill: replay merge evidence for tasks that merged before
    # this mapping was created so they are not stranded without evidence.
    try:
        backfilled = await ac.backfill_direction_merge_evidence(
            session, project_id, body.direction_key
        )
        if backfilled:
            logger.info(
                "[AC] link-direction backfill: direction=%s recorded=%d",
                body.direction_key,
                backfilled,
            )
    except Exception:
        logger.warning(
            "[AC] link-direction backfill failed for direction %s (non-fatal)",
            body.direction_key,
            exc_info=True,
        )

    return {
        "status": "ok",
        "direction_key": body.direction_key,
        "criterion_keys": deduped_keys,
    }


class AttestRequest(BaseModel):
    reason: str = Field(...)


@router.post("/projects/{project_id}/acceptance-criteria/{criterion_key}/attest")
async def attest_criterion(
    project_id: uuid.UUID,
    criterion_key: str,
    body: AttestRequest,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Record an operator attestation that a stuck criterion is intentionally accepted.

    This waiver path allows operators to unblock qualification when a criterion is
    permanently uncoverable (e.g. its mapped direction retired).  The attestation
    is operator-only and is never called by autonomous agents.

    Operator attestation is a lower-confidence, TIME-BOUNDED coverage tier: the
    recorded ``detail["attested_at"]`` timestamp starts an expiry timer
    (:data:`app.acceptance_criteria.ATTESTATION_WINDOW_DAYS`).  A re-attestation
    of an already-attested criterion is an UPSERT that refreshes ``attested_at``
    (and ``reason``), so the operator can renew the waiver before it expires.
    The returned ``coverage`` is ``"attested"`` — distinct from the
    machine-verified ``"covered"`` tier.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    from app.release_version import current_open_version

    open_version = await current_open_version(session, project_id)
    if open_version is None:
        raise HTTPException(status_code=409, detail="No open release version")
    open_version_id = open_version.id

    if not body.reason or not body.reason.strip():
        raise HTTPException(status_code=400, detail="reason must not be empty")

    existing = await ac.get_criteria_by_keys(
        session,
        project_id,
        [criterion_key],
        release_version_id=open_version_id,
    )
    if not existing:
        raise HTTPException(
            status_code=404,
            detail=f"Criterion key not found in open version: {criterion_key}",
        )

    row = existing[0]
    if row.status != "active":
        raise HTTPException(
            status_code=409,
            detail=f"Criterion '{criterion_key}' is {row.status} and cannot be attested",
        )

    # Capture primitive values before the try/except so that a rollback (which
    # expires the ORM instance on asyncpg) cannot trigger a sync lazy-load on
    # row.id or row.text — those reads would raise MissingGreenlet in production.
    criterion_id: uuid.UUID = row.id
    criterion_text: str = row.text

    attested_at = _utcnow().isoformat()
    detail = {"reason": body.reason, "attested_at": attested_at}

    # Upsert: try to INSERT a fresh attestation; on the uq_criterionevidence_dedup
    # collision (a repeat attestation by the same operator on the same criterion —
    # source_id=user_id is non-null so the partial unique index applies) refresh
    # the existing row's ``detail`` with a direct UPDATE so the expiry timer
    # restarts.  This is the operator re-attest / renew path — the waiver is
    # renewable before it expires, rather than a silent no-op.
    evidence = CriterionEvidence(
        criterion_id=criterion_id,
        project_id=project_id,
        source_kind="operator_attestation",
        source_id=current_user["user_id"],
        detail=detail,
    )
    session.add(evidence)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        # Refresh the existing attestation's detail (attested_at + reason) via a
        # Core UPDATE keyed on the dedup columns, so re-attestation renews the
        # expiry timer without creating a duplicate row.
        await session.execute(
            sa_update(CriterionEvidence)
            .where(
                CriterionEvidence.criterion_id == criterion_id,
                CriterionEvidence.source_kind == "operator_attestation",
                CriterionEvidence.source_id == current_user["user_id"],
            )
            .values(detail=detail)
        )
        await session.commit()
        logger.info(
            "[AC] Operator attestation refreshed: project=%s criterion=%s user=%s "
            "attested_at=%s reason=%r",
            project_id,
            criterion_key,
            current_user["user_id"],
            attested_at,
            body.reason,
        )

        # Best-effort: spawn a fresh corroboration task on re-attestation so the
        # system keeps trying to earn real coverage, not just extending the waiver.
        await ac.spawn_corroboration_task(
            session,
            project_id,
            criterion_key,
            criterion_text,
            body.reason,
            release_version_id=open_version_id,
        )

        return {"status": "ok", "criterion_key": criterion_key, "coverage": "attested"}

    # Best-effort: spawn a research task targeting this criterion so the system
    # can attempt to earn real machine-verified coverage (transition from
    # 'attested' to 'covered') rather than relying solely on the time-bounded
    # waiver. Failure here never blocks the attestation itself.
    await ac.spawn_corroboration_task(
        session,
        project_id,
        criterion_key,
        criterion_text,
        body.reason,
        release_version_id=open_version_id,
    )

    logger.info(
        "[AC] Operator attestation recorded: project=%s criterion=%s user=%s "
        "attested_at=%s reason=%r",
        project_id,
        criterion_key,
        current_user["user_id"],
        attested_at,
        body.reason,
    )
    return {"status": "ok", "criterion_key": criterion_key, "coverage": "attested"}


class WalkthroughE2EResult(BaseModel):
    criterion_key: str = Field(..., max_length=64)
    passed: bool
    notes: str | None = Field(default=None, max_length=2000)


class WalkthroughE2EEvidenceRequest(BaseModel):
    candidate_sha: str = Field(..., max_length=64)
    results: List[WalkthroughE2EResult] = Field(..., max_length=50)
    qualification_attempt_id: str | None = Field(default=None, max_length=64)
    # SHA the candidate stack ACTUALLY served (from its GET /api/version), as
    # observed by the agent that booted the ephemeral candidate stack. Additive
    # and optional for backward compatibility; recorded on each evidence row so a
    # later guard can enforce exercised_engine_sha == candidate_sha (the seam that
    # proves the walkthrough exercised the candidate engine, not the deployed one).
    exercised_engine_sha: str | None = Field(default=None, max_length=64)


@router.post("/projects/{project_id}/acceptance-criteria/walkthrough-e2e-evidence")
async def ingest_walkthrough_e2e_evidence(
    project_id: uuid.UUID,
    body: WalkthroughE2EEvidenceRequest,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Ingest per-criterion ``walkthrough_e2e`` results as SHA-pinned evidence.

    Written by the internal qualifier-spawned ``walkthrough_e2e`` task after it
    runs the live-stack browser walkthrough against a pinned candidate commit.
    Each result maps a ``criterion_key`` to a pass/fail; passing criteria earn a
    covering :data:`app.acceptance_criteria.WALKTHROUGH_E2E_EVIDENCE_KIND`
    evidence row on the open release version.

    **CRITICAL SAFETY:** covered-tier evidence is written ONLY when the posted
    ``candidate_sha`` matches the version's qualifying candidate SHA. A missing or
    mismatched SHA is rejected with 409 so a run against the wrong commit cannot
    forge coverage.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    from app.release_version import current_open_version

    open_version = await current_open_version(session, project_id)
    if open_version is None:
        raise HTTPException(status_code=409, detail="No open release version")

    summary = await ac.record_walkthrough_e2e_evidence(
        session,
        open_version,
        candidate_sha=body.candidate_sha,
        results=[r.model_dump() for r in body.results],
        qualification_attempt_id=body.qualification_attempt_id,
        exercised_engine_sha=body.exercised_engine_sha,
    )
    if summary.get("status") == "rejected":
        # SHA-mismatch / missing SHA: reject loudly so a walkthrough against the
        # wrong (or unpinned) commit cannot silently forge coverage.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "walkthrough_e2e_sha_rejected",
                "reason": summary.get("rejected_reason"),
                "candidate_sha": summary.get("candidate_sha"),
            },
        )

    logger.info(
        "[AC] walkthrough_e2e evidence ingested: project=%s version=%s recorded=%d "
        "skipped_not_passed=%d unmatched=%s",
        project_id,
        open_version.id,
        summary.get("recorded"),
        summary.get("skipped_not_passed"),
        summary.get("unmatched_keys"),
    )
    return {
        "status": "ok",
        "release_version_id": str(open_version.id),
        "recorded": summary.get("recorded", 0),
        "skipped_not_passed": summary.get("skipped_not_passed", 0),
        "unmatched_keys": summary.get("unmatched_keys", []),
        "candidate_sha": summary.get("candidate_sha"),
    }


@router.post(
    "/projects/{project_id}/acceptance-criteria/operator-walkthrough-evidence"
)
async def ingest_operator_walkthrough_evidence(
    project_id: uuid.UUID,
    body: WalkthroughE2EEvidenceRequest,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Ingest operator-run walkthrough results as covered-tier evidence.

    Unlike the qualifier-spawned ``walkthrough_e2e`` endpoint, this accepts a
    ``candidate_sha`` that does NOT need to match the version's qualifying SHA
    — the operator has intentionally pinned the walkthrough against a chosen
    commit and asserts the SHA is correct. Only a missing/empty SHA is rejected.

    Each result maps a ``criterion_key`` to a pass/fail; passing criteria earn a
    covering :data:`app.acceptance_criteria.WALKTHROUGH_E2E_EVIDENCE_KIND`
    evidence row on the open release version.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    from app.release_version import current_open_version

    open_version = await current_open_version(session, project_id)
    if open_version is None:
        raise HTTPException(status_code=409, detail="No open release version")

    summary = await ac.record_operator_walkthrough_evidence(
        session,
        open_version,
        candidate_sha=body.candidate_sha,
        results=[r.model_dump() for r in body.results],
    )
    if summary.get("status") == "rejected":
        # Empty/missing SHA: reject loudly so an unpinned walkthrough cannot
        # silently forge coverage.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "operator_walkthrough_sha_rejected",
                "reason": summary.get("rejected_reason"),
                "candidate_sha": summary.get("candidate_sha"),
            },
        )

    logger.info(
        "[AC] operator walkthrough evidence ingested: project=%s version=%s "
        "recorded=%d skipped_not_passed=%d unmatched=%s",
        project_id,
        open_version.id,
        summary.get("recorded"),
        summary.get("skipped_not_passed"),
        summary.get("unmatched_keys"),
    )
    return {
        "status": "ok",
        "release_version_id": str(open_version.id),
        "recorded": summary.get("recorded", 0),
        "skipped_not_passed": summary.get("skipped_not_passed", 0),
        "unmatched_keys": summary.get("unmatched_keys", []),
        "candidate_sha": summary.get("candidate_sha"),
    }


class ReconcileBackfillRequest(BaseModel):
    direction_key: str | None = Field(
        None,
        description="Specific direction to backfill. Omit to backfill all directions.",
    )


@router.post("/projects/{project_id}/acceptance-criteria/reconcile-backfill")
async def reconcile_backfill(
    project_id: uuid.UUID,
    body: ReconcileBackfillRequest,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Run the merge-evidence backfill on demand for stranded criterion mappings.

    Backfills evidence for already-merged tasks in the given direction (or all
    directions in the project when ``direction_key`` is omitted).  Idempotent
    — duplicate evidence rows are silently skipped.  Returns a per-direction
    summary of newly recorded rows.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    # Metric-derived acceptance-window coverage: re-score the version's
    # acceptance-window aggregate and, when it is genuinely non-zero, record a
    # machine-probe evidence row for the criteria that track that metric. This
    # grants the lower-confidence, time-bounded ``'probed'`` tier (the sweep ran)
    # — NOT full machine-verified ``'covered'``. Best-effort and idempotent —
    # runs on every reconcile so an operator can force it manually.
    from app.release_version import current_open_version

    open_version = await current_open_version(session, project_id)
    window_evidence = 0
    if open_version is not None:
        window_evidence = await ac.record_acceptance_window_coverage_evidence(
            session, open_version
        )

    # Rollup-child changed-files reconcile: already-landed rollup children merged
    # via a parent PR never received the parent's GitHub-verified _pr_changed_files,
    # so their declared named_test cannot be located in a diff and they are stuck at
    # declared_unsatisfied.  Runs BEFORE the unverified-merge backfill so a child
    # newly stamped here is picked up by that pass within the SAME reconcile run and
    # earns criterion evidence.  Best-effort and idempotent (setdefault stamping).
    rollup_child_changed_files_reconciled = await ac.backfill_rollup_child_changed_files(
        session, project_id, direction_key=body.direction_key
    )

    # Unverified-merge backfill: tasks whose staging merge fell open could never
    # record evidence via record_task_merge_evidence (because the verdict is
    # proven=False).  This runs in parallel with the direction merge evidence
    # backfill to give those tasks a recovery path to criterion coverage.
    unverified_evidence = await ac.backfill_unverified_merge_evidence(
        session, project_id, direction_key=body.direction_key
    )

    if body.direction_key is not None:
        # Backfill a single direction.
        rows = await ac.backfill_direction_merge_evidence(
            session, project_id, body.direction_key
        )
        return {
            "status": "ok",
            "results": [{"direction_key": body.direction_key, "evidence_recorded": rows}],
            "total_evidence_recorded": rows,
            "acceptance_window_evidence_recorded": window_evidence,
            "unverified_merge_evidence_recorded": unverified_evidence,
            "rollup_child_changed_files_reconciled": rollup_child_changed_files_reconciled,
        }

    # Backfill all directions in the project.
    from sqlmodel import select as _select
    from app.models import Direction as _Direction

    # Keyset-paginated query over direction keys to avoid loading thousands of
    # rows into memory at once. Batch size follows the same batching pattern
    # used elsewhere in the codebase for large table scans.
    _BATCH_SIZE = 500
    direction_keys = []
    last_key: str | None = None
    while True:
        q = (
            _select(_Direction.direction_key)
            .where(_Direction.project_id == project_id)
            .order_by(_Direction.direction_key)
            .limit(_BATCH_SIZE)
        )
        if last_key is not None:
            q = q.where(_Direction.direction_key > last_key)
        batch = list((await session.execute(q)).scalars().all())
        if not batch:
            break
        direction_keys.extend(batch)
        last_key = batch[-1]

    # Single unified Task scan for all directions instead of one scan per direction.
    totals_by_direction = await ac.backfill_all_directions_merge_evidence(
        session, project_id, direction_keys
    )
    results = []
    total = 0
    for dk, recorded in totals_by_direction.items():
        total += recorded
        if recorded:
            results.append({"direction_key": dk, "evidence_recorded": recorded})

    logger.info(
        "[AC] reconcile-backfill: project=%s directions=%d total_recorded=%d "
        "acceptance_window_evidence=%d unverified_merge_evidence=%d "
        "rollup_child_changed_files_reconciled=%d",
        project_id,
        len(direction_keys),
        total,
        window_evidence,
        unverified_evidence,
        rollup_child_changed_files_reconciled,
    )
    return {
        "status": "ok",
        "results": results,
        "total_evidence_recorded": total,
        "acceptance_window_evidence_recorded": window_evidence,
        "unverified_merge_evidence_recorded": unverified_evidence,
        "rollup_child_changed_files_reconciled": rollup_child_changed_files_reconciled,
    }
