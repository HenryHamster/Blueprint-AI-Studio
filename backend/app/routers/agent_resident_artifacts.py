"""Task-bound retrieval of exact human-tool revisions for Resident turns."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.artifact_refs import resident_artifact_ref_grants
from app.database import get_session
from app.models import ArtifactRef, Task, TaskStatus
from app.routers.agent import verify_agent_token
from app.resident_agent_rate_limits import (
    resident_remote_rate_key,
    resident_task_token_rate_key,
)
from app.shared_state import limiter
from app.storage import generate_download_signed_url
from app.strategic_resident_agent_binding import (
    ResidentAgentAuthorityError,
    ResidentAgentBinding,
    ResidentAgentBindingError,
    ResidentAgentTaskNotFound,
    load_live_resident_agent_task_locked,
)
from app.strategic_resident_managed_task import MANAGED_TASK_CONTEXT_KEY
from app.strategic_resident_models import StrategicOperationReceipt


router = APIRouter(prefix="/api/agent/resident-turn", tags=["agent"])
MAX_ARTIFACT_RETRIEVALS_PER_TURN = 32
_ARTIFACT_RETRIEVAL_LEDGER_KEY = "_resident_artifact_ref_retrievals"


def _register_artifact_retrieval(task: Any, artifact_ref_id: uuid.UUID) -> bool:
    context = task.context if isinstance(task.context, dict) else {}
    raw = context.get(_ARTIFACT_RETRIEVAL_LEDGER_KEY, [])
    if not isinstance(raw, list):
        raise HTTPException(status_code=409, detail="ArtifactRef retrieval ledger is invalid")
    try:
        ledger = [str(uuid.UUID(str(item))) for item in raw]
    except (TypeError, ValueError):
        raise HTTPException(status_code=409, detail="ArtifactRef retrieval ledger is invalid") from None
    if len(ledger) > MAX_ARTIFACT_RETRIEVALS_PER_TURN:
        raise HTTPException(status_code=409, detail="ArtifactRef retrieval ledger is invalid")
    identity = str(artifact_ref_id)
    if len(ledger) >= MAX_ARTIFACT_RETRIEVALS_PER_TURN:
        raise HTTPException(status_code=429, detail="ArtifactRef retrieval budget exhausted")
    task.context = {**context, _ARTIFACT_RETRIEVAL_LEDGER_KEY: [*ledger, identity]}
    return True


def _agent_binding(token: dict[str, Any]) -> ResidentAgentBinding:
    try:
        return ResidentAgentBinding(
            task_id=uuid.UUID(str(token.get("sub"))),
            project_id=uuid.UUID(str(token.get("project_id"))),
            vm_instance_id=str(token.get("vm_instance_id") or ""),
            task_type=str(token.get("task_type") or ""),
            claim_nonce=str(token.get("claim_nonce") or ""),
        )
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=403, detail="complete Resident task token required"
        ) from None


async def _load_artifact_bound_task(
    session: AsyncSession,
    binding: ResidentAgentBinding,
) -> Task:
    """Load either the live Resident turn or its receipt-bound managed Task."""

    if binding.task_type == "strategic_resident_turn":
        return await load_live_resident_agent_task_locked(
            session,
            binding,
            allowed_statuses=frozenset({TaskStatus.IN_PROGRESS}),
        )
    task = (
        await session.execute(
            select(Task)
            .where(
                Task.id == binding.task_id,
                Task.project_id == binding.project_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if task is None:
        raise ResidentAgentTaskNotFound("managed Task not found")
    payload = task.first_payload or {}
    marker = (task.context or {}).get(MANAGED_TASK_CONTEXT_KEY)
    if (
        task.status != TaskStatus.IN_PROGRESS
        or task.created_by != "STRATEGIC_AGENT"
        or task.parent_task_id is not None
        or not binding.vm_instance_id
        or task.vm_instance_id != binding.vm_instance_id
        or payload.get("type") != binding.task_type
        or binding.task_type not in {"create", "debug"}
        or not isinstance(marker, dict)
        or marker.get("capability_id")
        not in {"task.code_change.create", "task.debug.create"}
    ):
        raise ResidentAgentBindingError("receipt-bound managed Task scope required")
    try:
        receipt_id = uuid.UUID(str(marker.get("receipt_id")))
        source_turn_id = uuid.UUID(str(marker.get("source_turn_task_id")))
    except (TypeError, ValueError):
        raise ResidentAgentBindingError("managed Task receipt lineage is invalid") from None
    receipt = (
        await session.execute(
            select(StrategicOperationReceipt).where(
                StrategicOperationReceipt.id == receipt_id,
                StrategicOperationReceipt.project_id == task.project_id,
                StrategicOperationReceipt.turn_task_id == source_turn_id,
            )
        )
    ).scalar_one_or_none()
    if (
        receipt is None
        or receipt.status not in {"applied", "reconciled"}
        or receipt.operation_id != marker.get("operation_id")
        or receipt.capability != marker.get("capability_id")
        or receipt.authority_generation != marker.get("authority_generation")
    ):
        raise ResidentAgentBindingError("managed Task receipt authority is invalid")
    return task


@router.get("/artifact")
@limiter.limit("600/minute", key_func=resident_remote_rate_key)
@limiter.limit("60/minute", key_func=resident_task_token_rate_key)
async def get_resident_turn_artifact(
    request: Request,
    artifact_ref_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
) -> dict[str, Any]:
    """Retrieve one exact shared tool revision for the live Resident turn."""

    binding = _agent_binding(token)
    try:
        task = await _load_artifact_bound_task(session, binding)
    except ResidentAgentTaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ResidentAgentAuthorityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ResidentAgentBindingError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        grants = resident_artifact_ref_grants(task)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if artifact_ref_id not in grants:
        raise HTTPException(status_code=404, detail="ArtifactRef not granted to turn")
    artifact_ref = (
        await session.execute(
            select(ArtifactRef).where(
                ArtifactRef.id == artifact_ref_id,
                ArtifactRef.project_id == task.project_id,
            )
        )
    ).scalar_one_or_none()
    if artifact_ref is None:
        raise HTTPException(status_code=404, detail="ArtifactRef not found")
    _register_artifact_retrieval(task, artifact_ref_id)
    retrieval = dict(artifact_ref.retrieval_metadata or {})
    snapshot = {
        "artifact_ref_id": str(artifact_ref.id),
        "project_id": str(artifact_ref.project_id),
        "tool_id": str(artifact_ref.tool_id),
        "kind": artifact_ref.kind,
        "revision": artifact_ref.revision,
        "checksum": artifact_ref.checksum,
        "author": artifact_ref.author_user_id,
        "timestamp": artifact_ref.created_at.isoformat(),
        "content": artifact_ref.content_snapshot or {},
    }
    storage_uri = str(retrieval.get("storage_uri") or "").strip()
    # The immutable row and frozen grant are copied above. Commit the read-only
    # authority transaction now so Project/Resident/Task locks are never held
    # across IAM/GCS signed-URL generation.
    await session.commit()
    if snapshot["kind"] == "whiteboard":
        if not storage_uri:
            raise HTTPException(status_code=409, detail="Rendered image is unavailable")
        try:
            retrieval["download_url"] = await generate_download_signed_url(
                storage_uri, expiration_minutes=15
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="Artifact storage unavailable"
            ) from exc
    return {**snapshot, "retrieval": retrieval}


__all__ = ["MAX_ARTIFACT_RETRIEVALS_PER_TURN", "router"]
