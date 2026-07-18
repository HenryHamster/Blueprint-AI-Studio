"""Task-bound typed capability invocations for authoritative Resident turns."""

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import ProjectMessage, TaskStatus
from app.routers.agent import verify_agent_token
from app.shared_state import limiter
from app.resident_agent_rate_limits import (
    resident_remote_rate_key,
    resident_task_token_rate_key,
)
from app.strategic_operation_authority import execute_fenced_db_operation
from app.strategic_operation_external_effect import execute_external_effect_operation
from app.strategic_resident_external_effect_adapters import (
    EXTERNAL_EFFECT_SERVICES,
    build_external_effect,
    external_effect_operation_kind,
)
from app.strategic_operation_authority_validation import (
    AuthorityBoundaryMutation,
    AuthorityGenerationMismatch,
    ExpectedRevisionMismatch,
    FencedOperationRequest,
    FencedOperationUsageError,
    InactiveTurnAuthority,
    MutationPlanTooLarge,
    OperationIdentityCollision,
    OperationPayloadTooLarge,
    OperationResultTooLarge,
    ResidentEngineOwnerMismatch,
    StrategicAuthorityError,
    UnsupportedRevisionKey,
)
from app.strategic_resident_agent_binding import (
    ResidentAgentAuthorityError,
    ResidentAgentBinding,
    ResidentAgentBindingError,
    ResidentAgentTaskNotFound,
    load_live_resident_agent_task_locked,
)
from app.strategic_resident_capabilities import CapabilityMode, capability_for
from app.strategic_resident_capability_invocation import (
    CapabilityEvidenceRef,
    ResidentCapabilityInvocation,
    validated_capability_parameters,
)
from app.strategic_resident_evidence_policy import (
    INSPECTION_CAPABILITY_KINDS,
    SUCCESSFUL_INSPECTION_STATUSES,
    EvidenceKey,
    evidence_key,
)
from app.strategic_resident_inspection_receipt import (
    TURN_CONTEXT_HASH_KEY,
    build_inspection_receipt,
    citable_inspection_evidence,
)
from app.strategic_resident_models import StrategicOperationReceipt
from app.strategic_resident_operation_approval import (
    OperatorApprovalRequired,
    capability_requires_operator_approval,
    derive_approval_key,
    verify_and_consume_operation_approval,
)
from app.strategic_resident_project_field_mutation import (
    PROJECT_FIELD_MUTATION_SERVICE,
    project_field_mutation_callback,
    project_field_mutation_operation_kind,
)
from app.strategic_resident_task_mutation import (
    TASK_MUTATION_SERVICE,
    task_mutation_callback,
    task_mutation_operation_kind,
)
from app.strategic_resident_task_lifecycle import (
    build_task_lifecycle_effect,
    task_lifecycle_operation_kind,
)
from app.strategic_resident_task_inspection import (
    ResidentTaskNotFound,
    inspect_project_task,
    inspect_project_task_lineage,
)


router = APIRouter(prefix="/api/agent/resident-turn", tags=["agent"])

_INSPECTION_OPERATION_KINDS = frozenset(INSPECTION_CAPABILITY_KINDS.values())


def _build_receipt_evidence_set(
    receipts: Iterable[StrategicOperationReceipt],
) -> frozenset[EvidenceKey]:
    """Return the set of (kind, source_id, revision, checksum) tuples from this turn's receipts."""
    entries: set[EvidenceKey] = set()
    for receipt in receipts:
        if (
            str(receipt.operation_kind) not in _INSPECTION_OPERATION_KINDS
            or str(receipt.status) not in SUCCESSFUL_INSPECTION_STATUSES
        ):
            continue
        body = receipt.receipt or {}
        if not isinstance(body, Mapping):
            continue
        structured_result = body.get("structured_result")
        if not isinstance(structured_result, Mapping):
            continue
        receipt_evidence = structured_result.get("evidence")
        if not isinstance(receipt_evidence, list):
            continue
        for item in receipt_evidence:
            if not isinstance(item, Mapping):
                continue
            key = evidence_key(item, allow_source_aliases=False)
            if key is not None:
                entries.add(key)
    return frozenset(entries)


async def _check_evidence_refs_against_receipts(
    session: AsyncSession,
    binding: ResidentAgentBinding,
    evidence: tuple[CapabilityEvidenceRef, ...],
) -> None:
    """Reject any evidence ref not backed by a real inspection receipt for this turn.

    Rolls back the session on exit so the caller's session is clean for
    execute_fenced_db_operation, which opens its own independent transaction.
    """
    if not evidence:
        return
    try:
        rows = (
            await session.execute(
                select(StrategicOperationReceipt).where(
                    StrategicOperationReceipt.project_id == binding.project_id,
                    StrategicOperationReceipt.turn_task_id == binding.task_id,
                )
            )
        ).scalars().all()
        trusted = _build_receipt_evidence_set(rows)
        for ref in evidence:
            if (ref.kind, ref.source_id, ref.revision, ref.checksum) not in trusted:
                raise HTTPException(
                    status_code=403,
                    detail="capability evidence ref is not backed by a real inspection for this turn",
                )
    finally:
        await session.rollback()

MAX_CAPABILITY_REQUESTS_PER_TURN = 8
_CAPABILITY_REQUEST_LEDGER_CONTEXT_KEY = "_strategic_resident_capability_requests"

_TASK_INSPECTION_KINDS = {
    "task.inspect": "task",
    "task.logs.inspect": "logs",
    "task.failure.inspect": "failure",
    "task.provider.inspect": "provider",
}


class ResidentCapabilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_id: str = Field(min_length=3, max_length=120)
    parameters: dict[str, Any] = Field(default_factory=dict)


def _binding(token: dict[str, Any]) -> ResidentAgentBinding:
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


def _request_fingerprint(
    capability_id: str,
    parameters: dict[str, Any],
) -> str:
    encoded = json.dumps(
        {"capability_id": capability_id, "parameters": parameters},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _register_capability_request(
    task: Any,
    *,
    fingerprint: str,
) -> bool:
    """Consume one durable unique-call slot under the locked turn Task."""

    context = task.context if isinstance(task.context, dict) else {}
    raw_ledger = context.get(_CAPABILITY_REQUEST_LEDGER_CONTEXT_KEY, [])
    if not isinstance(raw_ledger, list) or any(
        not isinstance(item, str)
        or len(item) != 64
        or any(character not in "0123456789abcdef" for character in item)
        for item in raw_ledger
    ):
        raise HTTPException(status_code=409, detail="capability request ledger is invalid")
    ledger = list(dict.fromkeys(raw_ledger))
    if len(ledger) > MAX_CAPABILITY_REQUESTS_PER_TURN:
        raise HTTPException(status_code=409, detail="capability request ledger is invalid")
    if fingerprint in ledger:
        return False
    if len(ledger) >= MAX_CAPABILITY_REQUESTS_PER_TURN:
        raise HTTPException(status_code=429, detail="Resident capability budget exhausted")
    task.context = {
        **context,
        _CAPABILITY_REQUEST_LEDGER_CONTEXT_KEY: [*ledger, fingerprint],
    }
    return True


_capability_rate_key = resident_task_token_rate_key


async def _validate_approval_ref(
    session: AsyncSession,
    approval_ref: str | None,
    project_id: uuid.UUID,
) -> None:
    """Reject mutations whose approval_ref is missing, foreign, or agent-authored."""

    if not approval_ref:
        raise HTTPException(
            status_code=403,
            detail="capability requires an explicit operator approval_ref",
        )
    try:
        approval_id = uuid.UUID(approval_ref)
    except ValueError:
        raise HTTPException(
            status_code=403,
            detail="approval_ref must be a valid UUID",
        ) from None
    result = await session.execute(
        select(ProjectMessage).where(
            ProjectMessage.id == approval_id,
            ProjectMessage.project_id == project_id,
            ProjectMessage.from_type == "user",
        ).limit(1)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=403,
            detail="approval_ref does not reference a valid human operator message for this project",
        )


@router.post("/capability")
@limiter.limit("600/minute", key_func=resident_remote_rate_key)
@limiter.limit("60/minute", key_func=_capability_rate_key)
async def invoke_resident_capability(
    request: Request,
    body: ResidentCapabilityRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
) -> dict[str, Any]:
    """Invoke one implemented read-only capability under the live turn token."""

    binding = _binding(token)
    try:
        spec = capability_for(body.capability_id)
        parameters = validated_capability_parameters(spec, body.parameters)
    except OperationPayloadTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except (StrategicAuthorityError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if spec.mode is not CapabilityMode.inspect:
        raise HTTPException(
            status_code=501,
            detail="mutation capability is proposal-only until a fenced adapter exists",
        )
    if spec.service != "resident_task_inspection":
        raise HTTPException(
            status_code=501,
            detail="capability is declared but has no task-bound adapter",
        )

    try:
        task = await load_live_resident_agent_task_locked(
            session,
            binding,
            allowed_statuses=frozenset({TaskStatus.IN_PROGRESS}),
        )
    except ResidentAgentTaskNotFound as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ResidentAgentAuthorityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ResidentAgentBindingError as exc:
        await session.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    target_task_id = uuid.UUID(parameters["task_id"])
    fingerprint = _request_fingerprint(body.capability_id, parameters)
    try:
        request_registered = _register_capability_request(
            task,
            fingerprint=fingerprint,
        )
        if request_registered:
            # Make the ledger write (and its updated_at onupdate) visible before
            # inspecting the turn Task itself, so returned revision evidence is
            # not stale immediately after commit.
            await session.flush()
        if body.capability_id == "task.lineage.inspect":
            evidence = await inspect_project_task_lineage(
                session,
                project_id=task.project_id,
                task_id=target_task_id,
            )
        else:
            evidence = await inspect_project_task(
                session,
                project_id=task.project_id,
                task_id=target_task_id,
                kind=_TASK_INSPECTION_KINDS[body.capability_id],
            )
        evidence_json = evidence.model_dump(mode="json")
        # First time this inspection is registered this turn, persist a
        # server-authored receipt so the Resident may cite the evidence. The
        # ledger's one-write-per-fingerprint guarantee keeps operation_id unique;
        # the receipt row does not advance the turn task's updated_at.
        if request_registered:
            receipt = build_inspection_receipt(
                capability_id=body.capability_id,
                turn_task=task,
                context_hash=str((task.context or {}).get(TURN_CONTEXT_HASH_KEY) or ""),
                request_hash=fingerprint,
                evidence_json=evidence_json,
            )
            if receipt is not None:
                session.add(receipt)
        await session.commit()
    except ResidentTaskNotFound as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HTTPException:
        await session.rollback()
        raise
    except Exception:
        await session.rollback()
        raise
    return {
        "capability_id": body.capability_id,
        "evidence": citable_inspection_evidence(evidence_json),
    }


@router.post("/mutate")
@limiter.limit("600/minute", key_func=resident_remote_rate_key)
@limiter.limit("60/minute", key_func=_capability_rate_key)
async def invoke_resident_mutation(
    request: Request,
    body: ResidentCapabilityInvocation,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
) -> dict[str, Any]:
    """Apply one DB-only fenced strategic-memory mutation under the live turn token.

    The invocation envelope has already validated the capability declaration,
    parameter names/types/size, forbidden-key scope, ``operation_id``, and
    ``expected_revisions`` shape. Authority (project/turn/generation/CAS) is
    fenced by ``execute_fenced_db_operation`` in its own owned transaction.
    """

    binding = _binding(token)
    spec = capability_for(body.capability_id)
    if spec.mode is not CapabilityMode.mutate:
        raise HTTPException(status_code=400, detail="capability is not a mutation")
    # Resolve the fenced adapter: a DB-only Project-field / target-task mutation
    # (execute_fenced_db_operation), or an external-effect operation
    # (execute_external_effect_operation). External effects are gated by the
    # plan's own mechanisms — a wired adapter, evidence preconditions, authority
    # fencing, and (for explicit-approval capabilities) approval — plus the
    # master gate that nothing runs until an operator flips a project's engine
    # ownership to the Resident. A capability with no wired adapter stays 501.
    target_task_id: uuid.UUID | None = None
    callback = None
    operation_kind = None
    external_effect = None
    # Task-lifecycle mutations (cancel/requeue/retry/unblock) run the operator's
    # own modify_task_apply path inside the two-phase executor's perform phase —
    # so all the cascade (child cancellation, slot release, VM teardown) is reused
    # rather than re-implemented. Checked first because these span two declared
    # services but share one execution path.
    lifecycle_kind = task_lifecycle_operation_kind(body.capability_id)
    if lifecycle_kind is not None:
        operation_kind = lifecycle_kind
        external_effect = build_task_lifecycle_effect(
            body.capability_id,
            project_id=binding.project_id,
            parameters=dict(body.parameters),
        )
    elif spec.service in EXTERNAL_EFFECT_SERVICES:
        operation_kind = external_effect_operation_kind(body.capability_id)
        if operation_kind is not None:
            external_effect = build_external_effect(
                body.capability_id,
                project_id=binding.project_id,
                turn_task_id=binding.task_id,
                parameters=dict(body.parameters),
            )
    elif spec.service == PROJECT_FIELD_MUTATION_SERVICE:
        callback = project_field_mutation_callback(body.capability_id)
        operation_kind = project_field_mutation_operation_kind(body.capability_id)
    elif spec.service == TASK_MUTATION_SERVICE:
        callback = task_mutation_callback(body.capability_id)
        operation_kind = task_mutation_operation_kind(body.capability_id)
        if callback is not None:
            raw_target = body.parameters.get("task_id")
            if raw_target is None:
                raise HTTPException(
                    status_code=400,
                    detail="task mutation requires a task_id parameter",
                )
            target_task_id = uuid.UUID(str(raw_target))
    if operation_kind is None or (callback is None and external_effect is None):
        raise HTTPException(
            status_code=501,
            detail="mutation capability is proposal-only until a fenced adapter exists",
        )
    # Authority scope always comes from the signed turn token, never the body.
    if body.project_id != binding.project_id or body.turn_task_id != binding.task_id:
        raise HTTPException(
            status_code=403,
            detail="mutation envelope does not match the turn token authority",
        )
    # Explicit-approval capabilities require a durable operator approval scoped to
    # this exact operation (the key is derived from the parameters, so an approval
    # granted for one operation cannot authorise a different one). Consumed here
    # in the request transaction — idempotent per operation_id — before any effect.
    # This durable-approval gate (migration 0632, keyed on capability+parameters)
    # supersedes the earlier approval_ref-vs-ProjectMessage check on these same
    # capabilities; the operator grant creates an approval record, not a message,
    # so re-adding _validate_approval_ref here would 403 every real approval.
    if capability_requires_operator_approval(body.capability_id):
        try:
            await verify_and_consume_operation_approval(
                session,
                project_id=binding.project_id,
                capability=body.capability_id,
                approval_key=derive_approval_key(body.capability_id, body.parameters),
                operation_id=str(body.operation_id),
            )
            await session.commit()
        except OperatorApprovalRequired as exc:
            await session.rollback()
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    fenced_request = FencedOperationRequest(
        project_id=binding.project_id,
        turn_task_id=binding.task_id,
        authority_generation=body.authority_generation,
        operation_id=str(body.operation_id),
        operation_kind=operation_kind,
        capability=body.capability_id,
        risk=spec.risk.value,
        expected_revisions=dict(body.expected_revisions),
        request_payload=dict(body.parameters),
        target_task_id=target_task_id,
    )
    # Evidence refs supplied for a DB-fenced mutation must each be backed by a
    # real inspection receipt from this turn. External-effect operations are
    # governed by their own evidence preconditions + reconciliation-time trusted
    # evidence index, and run on a session shape this read-then-rollback check
    # does not apply to, so it is scoped to the fenced-DB path only.
    if external_effect is None:
        await _check_evidence_refs_against_receipts(session, binding, body.evidence)
    try:
        if external_effect is not None:
            receipt = await execute_external_effect_operation(
                session,
                fenced_request,
                perform=external_effect,
                effect_kind=operation_kind,
            )
        else:
            receipt = await execute_fenced_db_operation(
                session, fenced_request, callback
            )
    except OperationIdentityCollision as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        # A concurrent invocation with the same operation_id won the unique
        # (project_id, operation_id) insert first — the loser retries as a 409.
        raise HTTPException(
            status_code=409, detail="operation is already in progress"
        ) from exc
    except (
        AuthorityGenerationMismatch,
        ResidentEngineOwnerMismatch,
        InactiveTurnAuthority,
        ExpectedRevisionMismatch,
        AuthorityBoundaryMutation,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (
        MutationPlanTooLarge,
        OperationPayloadTooLarge,
        OperationResultTooLarge,
    ) as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except (
        FencedOperationUsageError,
        UnsupportedRevisionKey,
        StrategicAuthorityError,
        ValueError,
    ) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "capability_id": body.capability_id,
        "operation_id": receipt.operation_id,
        "status": receipt.status,
        "receipt_id": str(receipt.id),
    }


__all__ = [
    "MAX_CAPABILITY_REQUESTS_PER_TURN",
    "ResidentCapabilityRequest",
    "router",
]
