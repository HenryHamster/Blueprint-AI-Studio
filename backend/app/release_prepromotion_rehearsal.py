"""Durable dispatch and scoring for the trusted CI candidate build/smoke gate.

The workflow runs only on an ephemeral GitHub-hosted runner and deliberately
defers live-database and Hamsterwheel activation checks.  The backend accepts
only its own candidate-bound observation receipt and never performs GitHub I/O
while reading qualification evidence.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import github
from app.database import AsyncSessionLocal
from app.models import Project, ReleasePrepromotionDispatch, ReleaseVersion
from app.time_utils import _utcnow

PREPROMOTION_REHEARSAL_KIND = "blueprint.prepromotion_ci_build_smoke.v1"
PREPROMOTION_REHEARSAL_SCHEMA_VERSION = 1
PREPROMOTION_REHEARSAL_CONTRACT_TYPE = "prepromotion_ci_build_smoke"
PREPROMOTION_REHEARSAL_WORKFLOW = "prepromotion-rehearsal.yml"
PREPROMOTION_REHEARSAL_WORKFLOW_REF = "main"
PREPROMOTION_RECONCILE_GRACE_SECONDS = 120
PREPROMOTION_RECONCILE_MAX_SECONDS = 20
PREPROMOTION_RECONCILE_MAX_ATTEMPTS = 10
PREPROMOTION_RECONCILE_CLAIM_LEASE_SECONDS = 60
PREPROMOTION_REHEARSAL_CHECKS = (
    "candidate_checkout_exact_sha",
    "frontend_build_isolated",
    "frontend_manifest_verified",
    "candidate_image_built",
    "oci_revision_label_verified",
    "candidate_image_smoke_bounded",
)
PREPROMOTION_REHEARSAL_DEFERRED = (
    "live_database_migration_reachability",
    "hamsterwheel_activation",
)
PREPROMOTION_REHEARSAL_INPUTS: dict[str, str] = {
    # Dynamic candidate_sha/request_id are added at dispatch time. Keeping the
    # static portion empty makes accidental use of the production deploy inputs
    # impossible.
}
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")

# Evidence producers are ordered before the promotion gate. Structured
# metadata, never a fixed criterion key or prose marker, selects the trusted-CI
# producer.
_PREPROMOTION_GRADEABILITY_PHASES = {
    "qualification_setup": 10,
    "qualification_evidence": 20,
    "promotion_gate": 30,
}


def validate_prepromotion_gradeability(
    active_categories: list[Mapping[str, Any]],
    *,
    contracts: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Reject promotion criteria whose evidence producer is not pre-gradeable.

    Every active category can force ``not_met`` during qualification.  Ordinary
    categories are produced by the blind merge-grade in the qualification
    evidence phase; special machine-backed categories are selected by their
    closed evidence contract. A producer at or after the gate, or one with an
    unknown phase, is a circular/deadlocked contract and fails closed.
    """
    declared = contracts or {}
    for category in active_categories:
        key = str(
            category.get("category_key") or category.get("criterion_key") or ""
        ).strip()
        if not key:
            raise ValueError("active promotion criterion has no stable key")
        contract = declared.get(key)
        is_trusted_ci = (
            normalize_rehearsal_evidence_contract(
                category.get("evidence_contract")
            )
            is not None
        )
        if contract is None:
            contract = (
                {
                    "producer": "trusted_ci_candidate_build_smoke",
                    "producer_phase": "qualification_setup",
                    "gate_phase": "promotion_gate",
                    "prerequisites": ("durable_candidate_bound_outbox",),
                }
                if is_trusted_ci
                else {
                    "producer": "blind_merge_grade",
                    "producer_phase": "qualification_evidence",
                    "gate_phase": "promotion_gate",
                }
            )
        producer = str(contract.get("producer") or "").strip()
        producer_phase = str(contract.get("producer_phase") or "").strip()
        gate_phase = str(contract.get("gate_phase") or "").strip()
        if not producer:
            raise ValueError(f"criterion {key} has no evidence producer")
        if (
            producer_phase not in _PREPROMOTION_GRADEABILITY_PHASES
            or gate_phase not in _PREPROMOTION_GRADEABILITY_PHASES
        ):
            raise ValueError(
                f"criterion {key} has an unknown gradeability phase "
                f"({producer_phase!r} -> {gate_phase!r})"
            )
        if (
            _PREPROMOTION_GRADEABILITY_PHASES[producer_phase]
            >= _PREPROMOTION_GRADEABILITY_PHASES[gate_phase]
        ):
            raise ValueError(
                f"criterion {key} evidence producer {producer!r} runs at or "
                f"after its promotion gate"
            )
        if is_trusted_ci and (
            "durable_candidate_bound_outbox"
            not in contract.get("prerequisites", ())
        ):
            raise ValueError(
                f"criterion {key} is missing the durable candidate outbox "
                "precondition"
            )


def normalize_rehearsal_evidence_contract(raw: Any) -> dict[str, Any] | None:
    """Return the one explicit CI build/smoke evidence contract shape."""
    if not isinstance(raw, Mapping) or set(raw) != {"type", "predicate"}:
        return None
    predicate = raw.get("predicate")
    if (
        raw.get("type") != PREPROMOTION_REHEARSAL_CONTRACT_TYPE
        or not isinstance(predicate, Mapping)
        or set(predicate) != {"required"}
        or predicate.get("required") is not True
    ):
        return None
    return {
        "type": PREPROMOTION_REHEARSAL_CONTRACT_TYPE,
        "predicate": {"required": True},
    }


def rubric_requires_rehearsal_contract(criteria: list[Mapping[str, Any]]) -> bool:
    return any(
        normalize_rehearsal_evidence_contract(item.get("evidence_contract")) is not None
        for item in criteria
    )


async def dispatch_prepromotion_rehearsal(
    token: str,
    owner: str,
    repo: str,
    candidate_sha: str,
    request_id: str,
    *,
    workflow_ref: str = PREPROMOTION_REHEARSAL_WORKFLOW_REF,
    project_id: str | None = None,
) -> str:
    """Dispatch the trusted-main workflow for an immutable candidate request."""
    if not _SHA40_RE.fullmatch(candidate_sha):
        raise ValueError("candidate_sha must be full lowercase 40-hex")
    return await github.dispatch_workflow_run_outcome(
        token,
        owner,
        repo,
        PREPROMOTION_REHEARSAL_WORKFLOW,
        workflow_ref,
        project_id=project_id,
        inputs={"candidate_sha": candidate_sha, "request_id": request_id},
    )


def dispatch_projection(row: ReleasePrepromotionDispatch) -> dict[str, Any]:
    return {
        "status": row.status,
        "candidate_sha": row.candidate_sha,
        "request_id": str(row.id),
        "workflow": PREPROMOTION_REHEARSAL_WORKFLOW,
        "ref": row.workflow_ref,
        "workflow_run_id": row.workflow_run_id,
    }


async def prepare_rehearsal_dispatch_intent(
    session: AsyncSession,
    version: ReleaseVersion,
    candidate_sha: str,
    *,
    rearm_terminal: bool = False,
) -> tuple[bool, dict[str, Any]]:
    """Persist one candidate outbox intent; never perform external I/O."""
    row = (
        (
            await session.execute(
                select(ReleasePrepromotionDispatch).where(
                    ReleasePrepromotionDispatch.release_version_id == version.id,
                    ReleasePrepromotionDispatch.candidate_sha == candidate_sha,
                )
            )
        )
        .scalars()
        .first()
    )
    receipt = await persisted_rehearsal_for_version(
        session,
        version,
        candidate_sha,
    )
    if receipt is not None and row is not None and row.status == "observed":
        return True, {
            "status": "receipt_present",
            "candidate_sha": candidate_sha,
            "request_id": str(row.id),
        }
    if row is None:
        row = ReleasePrepromotionDispatch(
            release_version_id=version.id,
            project_id=version.project_id,
            candidate_sha=candidate_sha,
            workflow_ref=PREPROMOTION_REHEARSAL_WORKFLOW_REF,
            status="claimed",
        )
        session.add(row)
        await session.flush()
    elif rearm_terminal and row.status in {"rejected", "observed"}:
        row.status = "claimed"
        row.dispatch_attempts = 0
        row.attempted_at = None
        row.next_retry_at = None
        row.workflow_run_id = None
        row.last_error = None
        if version.prepromotion_rehearsal_receipt is not None:
            version.prepromotion_rehearsal_receipt = None
            session.add(version)
        session.add(row)
        await session.flush()
    # A workflow dispatch is not qualification evidence. Only the canonical
    # persisted receipt above can make the deterministic contract ready.
    return False, dispatch_projection(row)


async def _resolve_dispatch_github_context(project_id, session_factory):
    """Read credential metadata, close the DB session, then resolve secrets."""
    from app.models import UserIntegration

    async with session_factory() as session:
        project = await session.get(Project, project_id)
        if project is None or not project.github_owner or not project.github_repo:
            return None
        owner, repo, user_id = (
            project.github_owner,
            project.github_repo,
            project.user_id,
        )
        installation_id = None
        encrypted_pat = None
        if user_id:
            integration = (
                (
                    await session.execute(
                        select(UserIntegration).where(
                            UserIntegration.user_id == user_id
                        )
                    )
                )
                .scalars()
                .first()
            )
            if integration is not None:
                installation_id = integration.github_installation_id
                encrypted_pat = integration.github_pat

    token = None
    try:
        token = await github.get_repo_installation_token(owner, repo)
    except Exception:  # noqa: BLE001
        token = None
    if not token and installation_id:
        try:
            token = await github.generate_installation_access_token(
                installation_id, repositories=[repo]
            )
        except Exception:  # noqa: BLE001
            token = None
    if not token and encrypted_pat:
        try:
            from app.field_encryption import decrypt_field
            from app.secrets_manager import get_user_secret

            token = await get_user_secret(decrypt_field(encrypted_pat))
        except Exception:  # noqa: BLE001
            token = None
    return (token, owner, repo) if token else None


async def _cas_dispatch_result(
    dispatch_id,
    attempt: int,
    *,
    status: str,
    run_id: int | None = None,
    detail: str | None = None,
    receipt: dict[str, Any] | None = None,
    session_factory=AsyncSessionLocal,
) -> None:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(ReleasePrepromotionDispatch)
                .where(ReleasePrepromotionDispatch.id == dispatch_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            row is None
            or row.dispatch_attempts != attempt
            or row.status != "attempting"
        ):
            return
        version = await session.get(ReleaseVersion, row.release_version_id)
        if version is None:
            return
        active_candidate = (version.last_qualification or {}).get("candidate_sha")
        if version.status != "open" or active_candidate != row.candidate_sha:
            row.status = "rejected"
            row.last_error = "release candidate was superseded before observation"
            session.add(row)
            await session.commit()
            return
        project = await session.get(Project, row.project_id)
        repository = (
            f"{project.github_owner}/{project.github_repo}"
            if project is not None and project.github_owner and project.github_repo
            else None
        )
        existing_receipt = (
            persisted_rehearsal_for_candidate(
                version.prepromotion_rehearsal_receipt,
                row.candidate_sha,
                project_id=row.project_id,
                release_version_id=row.release_version_id,
                request_id=row.id,
                repository=repository,
            )
            if repository is not None
            else None
        )
        if (
            existing_receipt is not None
            and row.status == "observed"
            and row.workflow_run_id == existing_receipt["run_id"]
        ):
            status = "observed"
        elif receipt is not None and repository is not None:
            validated = validate_prepromotion_rehearsal_receipt(
                receipt,
                row.candidate_sha,
                project_id=row.project_id,
                release_version_id=row.release_version_id,
                request_id=row.id,
                repository=repository,
            )
            if validated is None:
                status = "uncertain"
                detail = "trusted workflow metadata did not produce a valid receipt"
            else:
                version.prepromotion_rehearsal_receipt = validated
                session.add(version)
                status = "observed"
        elif receipt is not None:
            status = "uncertain"
            detail = "project repository binding changed before observation"
        row.status = status
        if status in {"claimed", "dispatched", "uncertain"}:
            retry_attempt = max(1, row.dispatch_attempts)
            retry_seconds = min(
                3600,
                PREPROMOTION_RECONCILE_GRACE_SECONDS * (2 ** min(retry_attempt - 1, 5)),
            )
            row.next_retry_at = _utcnow() + timedelta(seconds=retry_seconds)
        else:
            row.next_retry_at = None
        row.workflow_run_id = run_id
        row.last_error = detail
        if status == "observed":
            row.observed_at = _utcnow()
        session.add(row)
        await session.commit()


async def _dispatch_pending_rehearsal_intents(
    *,
    session_factory=AsyncSessionLocal,
    limit: int = 10,
) -> int:
    """Claim, close DB, reconcile, then dispatch candidate rehearsal intents."""
    processed = 0
    deadline = time.monotonic() + PREPROMOTION_RECONCILE_MAX_SECONDS
    for _ in range(max(1, min(int(limit), 3))):
        if time.monotonic() >= deadline:
            break
        now = _utcnow()
        async with session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(ReleasePrepromotionDispatch)
                        .where(
                            ReleasePrepromotionDispatch.status.in_(
                                {"claimed", "attempting", "uncertain", "dispatched"}
                            ),
                            or_(
                                ReleasePrepromotionDispatch.next_retry_at.is_(None),
                                ReleasePrepromotionDispatch.next_retry_at <= now,
                            ),
                        )
                        .order_by(
                            func.coalesce(
                                ReleasePrepromotionDispatch.next_retry_at,
                                ReleasePrepromotionDispatch.claimed_at,
                            ).asc(),
                            ReleasePrepromotionDispatch.claimed_at.asc(),
                        )
                        .with_for_update(skip_locked=True)
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                break
            version = await session.get(ReleaseVersion, row.release_version_id)
            if version is None:
                await session.delete(row)
                await session.commit()
                continue
            active_candidate = (version.last_qualification or {}).get("candidate_sha")
            if version.status != "open" or active_candidate != row.candidate_sha:
                row.status = "rejected"
                row.next_retry_at = None
                row.last_error = "release candidate is no longer active"
                session.add(row)
                await session.commit()
                processed += 1
                continue
            project = await session.get(Project, row.project_id)
            repository = (
                f"{project.github_owner}/{project.github_repo}"
                if project is not None
                and project.github_owner
                and project.github_repo
                else None
            )
            existing_receipt = (
                persisted_rehearsal_for_candidate(
                    version.prepromotion_rehearsal_receipt,
                    row.candidate_sha,
                    project_id=row.project_id,
                    release_version_id=row.release_version_id,
                    request_id=row.id,
                    repository=repository,
                )
                if repository is not None
                else None
            )
            if (
                existing_receipt is not None
                and row.status == "observed"
                and row.workflow_run_id == existing_receipt["run_id"]
            ):
                row.status = "observed"
                row.observed_at = _utcnow()
                session.add(row)
                await session.commit()
                processed += 1
                continue
            prior_attempts = row.dispatch_attempts
            if prior_attempts >= PREPROMOTION_RECONCILE_MAX_ATTEMPTS:
                row.status = "rejected"
                row.next_retry_at = None
                row.last_error = "workflow observation attempt budget exhausted"
                session.add(row)
                await session.commit()
                processed += 1
                continue
            row.status = "attempting"
            row.dispatch_attempts += 1
            row.attempted_at = _utcnow()
            # Prevent another reaper from reclaiming this row while this
            # worker is resolving credentials or talking to GitHub. A crashed
            # worker becomes eligible again after the bounded lease.
            row.next_retry_at = row.attempted_at + timedelta(
                seconds=PREPROMOTION_RECONCILE_CLAIM_LEASE_SECONDS
            )
            attempt = row.dispatch_attempts
            snapshot = {
                "id": row.id,
                "project_id": row.project_id,
                "candidate_sha": row.candidate_sha,
                "workflow_ref": row.workflow_ref,
                "claimed_at": row.claimed_at,
            }
            session.add(row)
            await session.commit()

        remaining = max(0.001, deadline - time.monotonic())
        try:
            context = await asyncio.wait_for(
                _resolve_dispatch_github_context(
                    snapshot["project_id"], session_factory
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            break
        if context is None:
            await _cas_dispatch_result(
                snapshot["id"],
                attempt,
                status="claimed",
                detail="github credentials unavailable",
                session_factory=session_factory,
            )
            processed += 1
            continue
        token, owner, repo = context
        if prior_attempts:
            try:
                lookup = await asyncio.wait_for(
                    github.get_workflow_run_for_dispatch_request(
                        token,
                        owner,
                        repo,
                        PREPROMOTION_REHEARSAL_WORKFLOW,
                        snapshot["workflow_ref"],
                        str(snapshot["id"]),
                        snapshot["candidate_sha"],
                        project_id=str(snapshot["project_id"]),
                        max_pages=2,
                    ),
                    timeout=max(0.001, deadline - time.monotonic()),
                )
            except asyncio.TimeoutError:
                lookup = {"state": "unavailable", "detail": "workflow lookup timeout"}
            if lookup.get("state") == "found":
                status = str(lookup.get("status") or "")
                conclusion = lookup.get("conclusion")
                if status == "completed" and conclusion == "success":
                    async with session_factory() as receipt_session:
                        receipt_row = await receipt_session.get(
                            ReleasePrepromotionDispatch, snapshot["id"]
                        )
                    receipt = (
                        _canonical_ci_receipt(
                            receipt_row, f"{owner}/{repo}", lookup
                        )
                        if receipt_row is not None
                        else None
                    )
                    await _cas_dispatch_result(
                        snapshot["id"],
                        attempt,
                        status="observed" if receipt is not None else "uncertain",
                        run_id=lookup["run_id"],
                        detail=None
                        if receipt is not None
                        else "workflow metadata invalid",
                        receipt=receipt,
                        session_factory=session_factory,
                    )
                    processed += 1
                    continue
                if status == "completed":
                    await _cas_dispatch_result(
                        snapshot["id"],
                        attempt,
                        status="rejected",
                        run_id=lookup["run_id"],
                        detail=f"workflow concluded {conclusion or 'without success'}"[
                            :300
                        ],
                        session_factory=session_factory,
                    )
                    processed += 1
                    continue
                await _cas_dispatch_result(
                    snapshot["id"],
                    attempt,
                    status="dispatched",
                    run_id=lookup["run_id"],
                    session_factory=session_factory,
                )
                processed += 1
                continue
            # A prior POST may have succeeded even when GitHub's run listing
            # has not converged (or retained the run). Never manufacture a
            # second workflow run for an attempted request. A receipt or an
            # exact request-id run is the only safe reconciliation signal.
            detail = (
                "workflow run not observed after attempted dispatch"
                if lookup.get("state") == "not_found"
                else str(lookup.get("detail") or "workflow lookup unavailable")[:300]
            )
            await _cas_dispatch_result(
                snapshot["id"],
                attempt,
                status="uncertain",
                detail=detail,
                session_factory=session_factory,
            )
            processed += 1
            continue
        try:
            outcome = await asyncio.wait_for(
                dispatch_prepromotion_rehearsal(
                    token,
                    owner,
                    repo,
                    snapshot["candidate_sha"],
                    str(snapshot["id"]),
                    workflow_ref=snapshot["workflow_ref"],
                    project_id=str(snapshot["project_id"]),
                ),
                timeout=max(0.001, deadline - time.monotonic()),
            )
        except asyncio.TimeoutError:
            outcome = "ambiguous"
        status = {
            "accepted": "dispatched",
            "rejected": "rejected",
            "rejected_permanent_404": "rejected",
            "ambiguous": "uncertain",
        }[outcome]
        detail = None
        if outcome == "rejected_permanent_404":
            detail = (
                "permanent 404: dispatch target absent "
                f"{owner}/{repo}/actions/workflows/"
                f"{PREPROMOTION_REHEARSAL_WORKFLOW}@{snapshot['workflow_ref']}"
            )
        elif outcome != "accepted":
            detail = f"dispatch {outcome}"
        await _cas_dispatch_result(
            snapshot["id"],
            attempt,
            status=status,
            detail=detail,
            session_factory=session_factory,
        )
        processed += 1
    return processed


async def dispatch_pending_rehearsal_intents(
    *,
    session_factory=AsyncSessionLocal,
    limit: int = 10,
) -> int:
    """Run the complete DB and GitHub dispatch cycle within its hard wall.

    The internal per-request waits preserve the remaining budget for useful
    error details; this outer scope also covers pool checkout, row locks,
    commits, and context cleanup.
    """
    try:
        async with asyncio.timeout(PREPROMOTION_RECONCILE_MAX_SECONDS):
            return await _dispatch_pending_rehearsal_intents(
                session_factory=session_factory,
                limit=limit,
            )
    except TimeoutError:
        return 0


def validate_prepromotion_rehearsal_receipt(
    receipt: Any,
    candidate_sha: str | None,
    *,
    project_id: Any | None = None,
    release_version_id: Any | None = None,
    request_id: Any | None = None,
    repository: str | None = None,
) -> dict[str, Any] | None:
    """Validate the closed backend-authored trusted-CI receipt schema."""
    expected_sha = str(candidate_sha or "").strip()
    if not _SHA40_RE.fullmatch(expected_sha) or not isinstance(receipt, Mapping):
        return None
    expected_keys = {
        "kind",
        "schema_version",
        "source",
        "project_id",
        "release_version_id",
        "request_id",
        "candidate_sha",
        "repository",
        "workflow_file",
        "workflow_ref",
        "workflow_head_sha",
        "run_id",
        "run_attempt",
        "result",
        "checks",
        "deferred",
        "completed_at",
    }
    if set(receipt) != expected_keys:
        return None
    if (
        receipt.get("kind") != PREPROMOTION_REHEARSAL_KIND
        or receipt.get("schema_version") != PREPROMOTION_REHEARSAL_SCHEMA_VERSION
        or receipt.get("source") != "github_actions"
        or receipt.get("candidate_sha") != expected_sha
        or receipt.get("workflow_file") != PREPROMOTION_REHEARSAL_WORKFLOW
        or receipt.get("workflow_ref") != PREPROMOTION_REHEARSAL_WORKFLOW_REF
        or receipt.get("result") != "passed"
        or receipt.get("checks") != list(PREPROMOTION_REHEARSAL_CHECKS)
        or receipt.get("deferred") != list(PREPROMOTION_REHEARSAL_DEFERRED)
    ):
        return None
    for key in ("project_id", "release_version_id", "request_id"):
        try:
            uuid.UUID(str(receipt.get(key)))
        except (TypeError, ValueError, AttributeError):
            return None
    expected_ids = {
        "project_id": project_id,
        "release_version_id": release_version_id,
        "request_id": request_id,
    }
    for key, expected in expected_ids.items():
        if expected is not None and str(receipt.get(key)) != str(expected):
            return None
    receipt_repository = receipt.get("repository")
    if not isinstance(receipt_repository, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", receipt_repository
    ):
        return None
    if repository is not None and receipt_repository != repository:
        return None
    workflow_head_sha = receipt.get("workflow_head_sha")
    run_id = receipt.get("run_id")
    run_attempt = receipt.get("run_attempt")
    if (
        not isinstance(workflow_head_sha, str)
        or not _SHA40_RE.fullmatch(workflow_head_sha)
        or isinstance(run_id, bool)
        or not isinstance(run_id, int)
        or run_id <= 0
        or isinstance(run_attempt, bool)
        or not isinstance(run_attempt, int)
        or run_attempt <= 0
    ):
        return None
    completed_at = receipt.get("completed_at")
    if not isinstance(completed_at, str):
        return None
    try:
        completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if completed.tzinfo is None:
        return None
    return dict(receipt)


def _parse_github_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _canonical_ci_receipt(
    row: ReleasePrepromotionDispatch,
    repository: str,
    run: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Construct evidence only from an exact completed trusted-main run."""
    created_at = _parse_github_timestamp(run.get("created_at"))
    completed_at = _parse_github_timestamp(run.get("updated_at"))
    claimed_at = row.claimed_at
    if claimed_at.tzinfo is None:
        claimed_at = claimed_at.replace(tzinfo=timezone.utc)
    run_id = run.get("run_id")
    run_attempt = run.get("run_attempt")
    workflow_head_sha = run.get("head_sha")
    if (
        run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("event") != "workflow_dispatch"
        or run.get("head_branch") != PREPROMOTION_REHEARSAL_WORKFLOW_REF
        or run.get("path") != f".github/workflows/{PREPROMOTION_REHEARSAL_WORKFLOW}"
        or not isinstance(workflow_head_sha, str)
        or not _SHA40_RE.fullmatch(workflow_head_sha)
        or isinstance(run_id, bool)
        or not isinstance(run_id, int)
        or run_id <= 0
        or isinstance(run_attempt, bool)
        or not isinstance(run_attempt, int)
        or run_attempt <= 0
        or created_at is None
        or completed_at is None
        or created_at + timedelta(seconds=1) < claimed_at.astimezone(timezone.utc)
        or completed_at < created_at
        or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repository
        )
    ):
        return None
    return {
        "kind": PREPROMOTION_REHEARSAL_KIND,
        "schema_version": PREPROMOTION_REHEARSAL_SCHEMA_VERSION,
        "source": "github_actions",
        "project_id": str(row.project_id),
        "release_version_id": str(row.release_version_id),
        "request_id": str(row.id),
        "candidate_sha": row.candidate_sha,
        "repository": repository,
        "workflow_file": PREPROMOTION_REHEARSAL_WORKFLOW,
        "workflow_ref": PREPROMOTION_REHEARSAL_WORKFLOW_REF,
        "workflow_head_sha": workflow_head_sha,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "result": "passed",
        "checks": list(PREPROMOTION_REHEARSAL_CHECKS),
        "deferred": list(PREPROMOTION_REHEARSAL_DEFERRED),
        "completed_at": completed_at.isoformat(),
    }


def persisted_rehearsal_for_candidate(
    receipt: Any,
    candidate_sha: str | None,
    *,
    project_id: Any | None = None,
    release_version_id: Any | None = None,
    request_id: Any | None = None,
    repository: str | None = None,
) -> dict[str, Any] | None:
    """Read the persisted CI receipt as a gradeable, candidate-bound fact."""
    return validate_prepromotion_rehearsal_receipt(
        receipt,
        candidate_sha,
        project_id=project_id,
        release_version_id=release_version_id,
        request_id=request_id,
        repository=repository,
    )


async def persisted_rehearsal_for_version(
    session: AsyncSession,
    version: ReleaseVersion,
    candidate_sha: str | None,
) -> dict[str, Any] | None:
    """Return evidence only when its project, version, repo, and outbox all bind."""
    row = (
        await session.execute(
            select(ReleasePrepromotionDispatch).where(
                ReleasePrepromotionDispatch.release_version_id == version.id,
                ReleasePrepromotionDispatch.project_id == version.project_id,
                ReleasePrepromotionDispatch.candidate_sha == candidate_sha,
                ReleasePrepromotionDispatch.status == "observed",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    project = await session.get(Project, version.project_id)
    repository = (
        f"{project.github_owner}/{project.github_repo}"
        if project is not None and project.github_owner and project.github_repo
        else None
    )
    if repository is None:
        return None
    receipt = persisted_rehearsal_for_candidate(
        version.prepromotion_rehearsal_receipt,
        candidate_sha,
        project_id=version.project_id,
        release_version_id=version.id,
        request_id=row.id,
        repository=repository,
    )
    if receipt is None or row.workflow_run_id != receipt["run_id"]:
        return None
    return receipt


async def redrive_rehearsal_ready_qualification(
    version_id: Any,
    *,
    session_factory=None,
) -> bool:
    """Resume exactly the qualification parked for a now-observed CI receipt."""
    from app import release_qualifier

    factory = session_factory or AsyncSessionLocal
    async with factory() as session:
        version = await session.get(ReleaseVersion, version_id)
        if version is None or version.status != "open" or version.phase != "stabilize":
            return False
        qualification = version.last_qualification
        if (
            not isinstance(qualification, dict)
            or qualification.get("status") != "rehearsal_dispatch_pending"
        ):
            return False
        try:
            continuation_attempt_id = uuid.UUID(
                str(qualification.get("qualification_attempt_id"))
            )
        except (TypeError, ValueError, AttributeError):
            return False
        candidate_sha = qualification.get("candidate_sha")
        if (
            await persisted_rehearsal_for_version(session, version, candidate_sha)
            is None
        ):
            return False
        result = await release_qualifier.run_qualification(
            session,
            version.project_id,
            version,
            session_factory=factory,
            qualification_attempt_id=continuation_attempt_id,
            retry_episode=qualification.get("retry_episode", 0),
            retry_episode_cap=qualification.get("retry_episode_cap"),
            requested_probes=qualification.get("requested_probes"),
            auto_trigger=False,
        )
        await session.commit()
        return bool(result and result.get("status") == "spawned")


def is_rehearsal_acceptance_category(category: Mapping[str, Any]) -> bool:
    """Identify only an explicitly contracted trusted-CI criterion."""
    return (
        normalize_rehearsal_evidence_contract(category.get("evidence_contract"))
        is not None
    )


def rehearsal_category_evidence(
    receipt: Any,
    candidate_sha: str | None,
    categories: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Project the backend receipt only into explicitly contracted categories."""
    valid = persisted_rehearsal_for_candidate(receipt, candidate_sha)
    result: list[dict[str, Any]] = []
    for category in categories:
        if not is_rehearsal_acceptance_category(category):
            continue
        key = str(category.get("category_key") or category.get("criterion_key") or "")
        if not key:
            continue
        if valid is None:
            result.append(
                {
                    "category_key": key,
                    "status": "not_met",
                    "tier": "machine",
                    "gap": "trusted-main candidate build/smoke receipt is absent",
                    "citations": [],
                    "authoritative": True,
                    "evidence_provenance": {
                        "kind": "machine",
                        "source_kind": "release_record_validation",
                        "state": "missing_or_invalid",
                        "candidate_sha": candidate_sha,
                    },
                }
            )
            continue
        result.append(
            {
                "category_key": key,
                "status": "met",
                "tier": "machine",
                "gap": None,
                "citations": [
                    {
                        "kind": "test",
                        "ref": f"github-actions:{valid['run_id']}:{valid['request_id']}",
                    }
                ],
                "authoritative": True,
                "evidence_provenance": {
                    "kind": "machine",
                    "source_kind": "trusted_ci_workflow",
                    "request_id": valid["request_id"],
                    "run_id": valid["run_id"],
                    "candidate_sha": candidate_sha,
                },
            }
        )
    return result


def apply_rehearsal_evidence(
    category_verdicts: list[dict[str, Any]] | None,
    active_categories: list[Mapping[str, Any]],
    receipt: Any,
    candidate_sha: str | None,
) -> list[dict[str, Any]] | None:
    """Insert or replace deterministic verdicts for every contracted key."""
    machine_evidence = rehearsal_category_evidence(
        receipt, candidate_sha, active_categories
    )
    if not machine_evidence:
        return category_verdicts
    by_key = {
        str((verdict or {}).get("category_key") or ""): dict(verdict)
        for verdict in (category_verdicts or [])
        if isinstance(verdict, dict)
    }
    for evidence in machine_evidence:
        key = evidence["category_key"]
        prior = by_key.get(key, {})
        category = next(
            item
            for item in active_categories
            if str(item.get("category_key") or item.get("criterion_key") or "") == key
        )
        by_key[key] = {
            **prior,
            **{
                field: value
                for field, value in evidence.items()
                if field != "authoritative"
            },
            "text": category.get("text"),
        }
    ordered_keys = [
        str((verdict or {}).get("category_key") or "")
        for verdict in (category_verdicts or [])
        if isinstance(verdict, dict)
    ]
    ordered_keys.extend(
        item["category_key"]
        for item in machine_evidence
        if item["category_key"] not in ordered_keys
    )
    return [by_key[key] for key in ordered_keys if key in by_key]
