"""Project-scoped operator controls for Strategic Memory V2.

The router deliberately keeps semantic mutations small.  The only write verbs
are a named, idempotent ledger transition and native-message privacy deletion;
agents cannot invoke either one.  Every opaque identifier is resolved after
membership checks and within ``project_id`` so guessed cross-project IDs have
the same not-found response as unknown IDs.
"""

import uuid
import time
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.admin_helpers import _get_user_project_with_role
from app.auth import get_current_user
from app.database import get_session
from app.models import StrategicMemoryBackfillCursor
from app.shared_state import limiter
from app.strategic_memory_claim_redaction_repair import (
    ClaimRedactionRepairError,
    apply_false_fast_claim_redaction_repair,
    audit_false_fast_claim_redactions,
    current_serving_revision,
)
from app.strategic_memory_governance import (
    MemoryGovernanceError,
    MemoryObjectNotFound,
    apply_governed_memory_action,
    delete_native_project_message_governed,
    get_project_memory_detail,
    get_project_memory_fetch_trace,
    list_project_memory,
    memory_v2_rollout,
    record_memory_v2_metric,
)
from app.strategic_memory_runtime import (
    BackfillPreconditionError,
    OPERATOR_BACKFILL_BATCH_SIZE,
    PROJECT_MESSAGE_PRODUCER,
    backfill_project_message_receipts,
)
from app.time_utils import utc_iso


router = APIRouter(
    prefix="/api/admin",
    tags=["admin-strategic-memory"],
    dependencies=[Depends(get_current_user)],
)


class GovernedMemoryActionRequest(BaseModel):
    operation_id: str = Field(min_length=1, max_length=255)
    action: Literal["challenge", "supersede", "retract", "forget"]
    item_id: uuid.UUID
    expected_version: int = Field(ge=1)
    expected_ledger_revision: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=4_000)
    target_item_id: uuid.UUID | None = None
    target_version: int | None = Field(default=None, ge=1)
    effective_from: datetime | None = None
    effective_until: datetime | None = None


class MemoryBackfillRequest(BaseModel):
    """One explicitly bounded, resumable historical-ingestion page."""

    expected_cursor_id: uuid.UUID | None = None
    expected_last_sequence: int = Field(default=0, ge=0)


class FalseClaimRedactionRepairRequest(BaseModel):
    """Two-phase operator review contract for one historical repair set."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mode: Literal["dry_run", "apply"]
    window_start: datetime
    window_end: datetime
    buggy_serving_revision: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    expected_serving_revision: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    operation_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_candidate_count: int | None = Field(default=None, ge=0, le=250)
    expected_candidate_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def require_apply_fences(self) -> "FalseClaimRedactionRepairRequest":
        apply_fields = (
            self.operation_id,
            self.expected_candidate_count,
            self.expected_candidate_digest,
        )
        if self.mode == "apply" and any(value is None for value in apply_fields):
            raise ValueError(
                "apply requires operation_id, expected_candidate_count, and "
                "expected_candidate_digest"
            )
        if self.mode == "apply" and self.expected_candidate_count == 0:
            raise ValueError("apply requires at least one reviewed candidate")
        if self.mode == "dry_run" and any(value is not None for value in apply_fields):
            raise ValueError("dry_run does not accept apply-only fields")
        return self


def _require_read_enabled(project_id: uuid.UUID) -> None:
    if not memory_v2_rollout(project_id).read_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _not_found(error: MemoryObjectNotFound) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


async def _backfill_status(session: AsyncSession, project_id: uuid.UUID) -> dict:
    cursor = await session.scalar(
        select(StrategicMemoryBackfillCursor).where(
            StrategicMemoryBackfillCursor.project_id == project_id,
            StrategicMemoryBackfillCursor.producer == PROJECT_MESSAGE_PRODUCER,
        )
    )
    if cursor is None:
        return {
            "status": "not_started",
            "cursor_id": None,
            "cutoff_sequence": None,
            "last_sequence": 0,
            "scanned_count": 0,
            "admitted_count": 0,
            "skipped_count": 0,
            "has_error": False,
        }
    return {
        "status": cursor.status,
        "cursor_id": str(cursor.id),
        "cutoff_sequence": int((cursor.cutoff_spec or {}).get("max_sequence", 0) or 0),
        "last_sequence": int((cursor.cursor or {}).get("last_sequence", 0) or 0),
        "scanned_count": int(cursor.scanned_count),
        "admitted_count": int(cursor.admitted_count),
        "skipped_count": int(cursor.skipped_count),
        # Keep internal/provider error text out of the operator API.
        "has_error": bool(cursor.error),
    }


@router.get("/projects/{project_id}/memory")
@limiter.limit("60/minute")
async def admin_list_strategic_memory(
    request: Request,
    project_id: uuid.UUID,
    limit: int = Query(100, ge=1, le=250),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """List current project memory heads for a member or owner."""

    del request
    await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")
    _require_read_enabled(project_id)
    return {"items": await list_project_memory(session, project_id=project_id, limit=limit, offset=offset)}


@router.get("/projects/{project_id}/memory/fetch-traces/{snapshot_id}")
@limiter.limit("60/minute")
async def admin_get_strategic_memory_fetch_trace(
    request: Request,
    project_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Read content-free retrieval fencing metadata for one snapshot."""

    del request
    await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")
    _require_read_enabled(project_id)
    try:
        return await get_project_memory_fetch_trace(session, project_id=project_id, snapshot_id=snapshot_id)
    except MemoryObjectNotFound as error:
        raise _not_found(error) from error


@router.get("/projects/{project_id}/memory/rollout")
@limiter.limit("60/minute")
async def admin_get_strategic_memory_rollout(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Expose feature gates without embedding a provider choice in the API."""

    del request
    await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")
    rollout = memory_v2_rollout(project_id)
    return {
        "read_enabled": rollout.read_enabled,
        "shadow_enabled": rollout.shadow_enabled,
        "actions_enabled": rollout.actions_enabled,
        "backfill_enabled": rollout.backfill_enabled,
        "provider": None,
    }


@router.get("/projects/{project_id}/memory/backfill")
@limiter.limit("60/minute")
async def admin_get_strategic_memory_backfill(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Read content-free progress for the explicit historical backfill."""

    del request
    await _get_user_project_with_role(project_id, current_user["user_id"], session, "owner")
    return await _backfill_status(session, project_id)


@router.post("/projects/{project_id}/memory/backfill/advance")
@limiter.limit("6/minute")
async def admin_continue_strategic_memory_backfill(
    request: Request,
    project_id: uuid.UUID,
    body: MemoryBackfillRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Start or resume exactly one bounded historical-ingestion page."""

    started = time.monotonic()
    await _get_user_project_with_role(project_id, current_user["user_id"], session, "owner")
    if not memory_v2_rollout(project_id).backfill_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Memory backfill is not enabled",
        )
    try:
        result = await backfill_project_message_receipts(
            session,
            project_id=project_id,
            batch_size=OPERATOR_BACKFILL_BATCH_SIZE,
            enqueue=False,
            expected_cursor_id=body.expected_cursor_id,
            expected_last_sequence=body.expected_last_sequence,
        )
        await session.commit()
    except BackfillPreconditionError as error:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except Exception:
        await session.rollback()
        raise
    record_memory_v2_metric(
        "backfill_page",
        project_id=project_id,
        started_at=started,
        scanned=result.scanned,
        admitted=result.admitted,
        complete=result.complete,
    )
    return {
        **(await _backfill_status(session, project_id)),
        "page": {
            "scanned": result.scanned,
            "admitted": result.admitted,
            "skipped": result.skipped,
            "complete": result.complete,
            "queued_job_id": None,
        },
    }


@router.post("/projects/{project_id}/memory/repairs/fast-claim-redactions")
@limiter.limit("6/minute")
async def admin_repair_false_fast_claim_redactions(
    request: Request,
    project_id: uuid.UUID,
    body: FalseClaimRedactionRepairRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Review or apply the exact claim-metadata regression repair set.

    The first call is read-only and returns a content-free digest.  Applying
    requires that exact count/digest, an immutable operation id, and the
    currently served revision.  A changed row or rolling deploy therefore
    fails closed before any receipt is restored.
    """

    del request
    await _get_user_project_with_role(
        project_id,
        current_user["user_id"],
        session,
        "owner",
    )
    if current_serving_revision() != body.expected_serving_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Serving revision changed; repeat the repair review",
        )
    if body.buggy_serving_revision == body.expected_serving_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Repair review must run on a revision different from the buggy one",
        )
    try:
        if body.mode == "dry_run":
            audit = await audit_false_fast_claim_redactions(
                session,
                project_id=project_id,
                window_start=body.window_start,
                window_end=body.window_end,
            )
            return {
                "mode": "dry_run",
                "project_id": str(project_id),
                "serving_revision": current_serving_revision(),
                "buggy_serving_revision": body.buggy_serving_revision,
                "window_start": utc_iso(audit.window_start),
                "window_end": utc_iso(audit.window_end),
                "candidate_count": audit.candidate_count,
                "candidate_digest": audit.candidate_digest,
                "content_returned": False,
            }
        assert body.operation_id is not None
        assert body.expected_candidate_count is not None
        assert body.expected_candidate_digest is not None
        result = await apply_false_fast_claim_redaction_repair(
            session,
            project_id=project_id,
            actor_id=str(current_user["user_id"]),
            operation_id=body.operation_id,
            buggy_serving_revision=body.buggy_serving_revision,
            expected_serving_revision=body.expected_serving_revision,
            window_start=body.window_start,
            window_end=body.window_end,
            expected_candidate_count=body.expected_candidate_count,
            expected_candidate_digest=body.expected_candidate_digest,
        )
        await session.commit()
    except ClaimRedactionRepairError as error:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except Exception:
        await session.rollback()
        raise
    return {
        "mode": "apply",
        "project_id": str(project_id),
        "serving_revision": result.serving_revision,
        "operation_id": result.operation_id,
        "audit_source_id": str(result.audit_source_id),
        "candidate_count": result.audit.candidate_count,
        "candidate_digest": result.audit.candidate_digest,
        "restored_count": result.restored_count,
        "reconsolidation_pending_count": result.reconsolidation_pending_count,
        "queued_job_id": (
            str(result.queued_job_id) if result.queued_job_id is not None else None
        ),
        "replayed": result.replayed,
        "content_returned": False,
    }


@router.get("/projects/{project_id}/memory/{item_id}")
@limiter.limit("60/minute")
async def admin_get_strategic_memory_detail(
    request: Request,
    project_id: uuid.UUID,
    item_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Read all immutable versions and normalized provenance for one item."""

    del request
    await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")
    _require_read_enabled(project_id)
    try:
        return await get_project_memory_detail(session, project_id=project_id, item_id=item_id)
    except MemoryObjectNotFound as error:
        raise _not_found(error) from error


@router.post("/projects/{project_id}/memory/actions")
@limiter.limit("20/minute")
async def admin_apply_strategic_memory_action(
    request: Request,
    project_id: uuid.UUID,
    body: GovernedMemoryActionRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Append a reviewed semantic transition through the regular ledger gate."""

    started = time.monotonic()
    # Mutating semantic history has the same owner boundary as privacy
    # deletion.  Reads remain member-visible.
    await _get_user_project_with_role(project_id, current_user["user_id"], session, "owner")
    rollout = memory_v2_rollout(project_id)
    if not rollout.actions_enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Memory actions are not enabled")
    try:
        result = await apply_governed_memory_action(
            session,
            project_id=project_id,
            actor_id=str(current_user["user_id"]),
            **body.model_dump(),
        )
        await session.commit()
    except MemoryObjectNotFound as error:
        await session.rollback()
        raise _not_found(error) from error
    except MemoryGovernanceError as error:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    record_memory_v2_metric(
        "operator_action", project_id=project_id, started_at=started, action=body.action
    )
    return {
        "operation_id": result.operation_id,
        "ledger_revision": result.ledger_revision,
        "source_id": str(result.source_id),
        "job_id": str(result.job_id),
        "replayed": result.replayed,
    }


@router.delete("/projects/{project_id}/memory/messages/{message_id}")
@limiter.limit("10/minute")
async def admin_delete_native_memory_message(
    request: Request,
    project_id: uuid.UUID,
    message_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Owner-only native deletion with generation fence and derived-data purge."""

    del request
    await _get_user_project_with_role(project_id, current_user["user_id"], session, "owner")
    try:
        result = await delete_native_project_message_governed(
            session, project_id=project_id, message_id=message_id
        )
        await session.commit()
    except MemoryObjectNotFound as error:
        await session.rollback()
        raise _not_found(error) from error
    except MemoryGovernanceError as error:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return {
        "message_id": str(result.message_id),
        "privacy_generation": result.privacy_generation,
        "tombstoned_source_count": result.tombstoned_source_count,
        "suppressed_last_provenance_versions": result.suppressed_last_provenance_versions,
    }
