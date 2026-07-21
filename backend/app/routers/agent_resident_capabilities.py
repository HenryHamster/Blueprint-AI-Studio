"""Task-bound typed capability invocations for authoritative Resident turns."""

import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import Task, TaskStatus
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
from app.strategic_resident_capabilities import (
    INVOKABLE_CAPABILITY_IDS,
    CapabilityMode,
    capability_for,
)
from app.strategic_resident_capability_invocation import (
    CapabilityEvidenceRef,
    CorrectableCapabilityParameterError,
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
from app.strategic_resident_inspection_runtime import (
    MAX_CAPABILITY_REQUESTS_PER_TURN,
    canonical_inspection_replay,
    register_capability_request,
    request_fingerprint,
)
from app.strategic_resident_pr_inspection import (
    inspect_project_pr,
    invoke_pr_inspection,
)
from app.strategic_resident_pr_diff_inspection import (
    inspect_project_pr_diff,
    invoke_pr_diff_inspection,
)
from app.strategic_resident_pr_checks_inspection import (
    inspect_project_pr_checks,
    invoke_pr_checks_inspection,
)
from app.strategic_resident_runner_inspection import (
    ResidentRunnerNotFound,
    inspect_project_runner,
    inspect_project_runners,
)
from app.strategic_resident_project_inspection import (
    inspect_project_pipeline,
    inspect_project_qualification,
    invoke_project_inspection,
)
from app.strategic_resident_task_mutation import (
    TASK_MUTATION_SERVICE,
    task_mutation_callback,
    task_mutation_operation_kind,
)
from app.strategic_resident_managed_task import (
    MANAGED_TASK_CREATE_SERVICE,
    managed_task_create_callback,
    managed_task_create_operation_kind,
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

def _build_receipt_evidence_set(
    receipts: Iterable[StrategicOperationReceipt],
    *,
    expected_context_hash: str,
) -> frozenset[EvidenceKey]:
    """Return the set of (kind, source_id, revision, checksum) tuples from this turn's receipts."""
    entries: set[EvidenceKey] = set()
    for receipt in receipts:
        if (
            INSPECTION_CAPABILITY_KINDS.get(str(receipt.capability))
            != str(receipt.operation_kind)
            or str(receipt.status) not in SUCCESSFUL_INSPECTION_STATUSES
            or str((receipt.expected_revisions or {}).get("context_hash") or "")
            != expected_context_hash
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
        turn_context = await session.scalar(
            select(Task.context).where(
                Task.id == binding.task_id,
                Task.project_id == binding.project_id,
            )
        )
        context_hash = str(
            (turn_context or {}).get(TURN_CONTEXT_HASH_KEY) or ""
        )
        if not context_hash:
            raise HTTPException(
                status_code=409,
                detail="Resident turn context is not frozen for evidence use",
            )
        rows = (
            await session.execute(
                select(StrategicOperationReceipt).where(
                    StrategicOperationReceipt.project_id == binding.project_id,
                    StrategicOperationReceipt.turn_task_id == binding.task_id,
                )
            )
        ).scalars().all()
        trusted = _build_receipt_evidence_set(
            rows,
            expected_context_hash=context_hash,
        )
        for ref in evidence:
            if (ref.kind, ref.source_id, ref.revision, ref.checksum) not in trusted:
                raise HTTPException(
                    status_code=403,
                    detail="capability evidence ref is not backed by a real inspection for this turn",
                )
    finally:
        await session.rollback()

_MAX_PROJECTED_UNKNOWN_EFFECTS = 50


def _completion_projection(
    receipt: StrategicOperationReceipt,
    *,
    capability_id: str,
) -> dict[str, Any]:
    """Return a bounded final-result projection backed by the durable receipt."""

    receipt_id = str(receipt.id)
    operation_id = str(receipt.operation_id)
    receipt_status = str(receipt.status)
    operation_status = (
        receipt_status
        if receipt_status in {"applied", "reconciled", "failed"}
        else "applying"
    )
    unknown_effects: list[dict[str, str]] = []
    unknown_sources = getattr(receipt, "unknown_effects", None) or []
    for raw in list(unknown_sources)[:_MAX_PROJECTED_UNKNOWN_EFFECTS]:
        identity = (
            raw.get("effect_id", raw.get("id"))
            if isinstance(raw, Mapping)
            else raw
        )
        if identity is None:
            continue
        effect_id = str(identity)
        if not effect_id or len(effect_id) > 512:
            continue
        unknown_effects.append(
            {
                "effect_id": effect_id,
                "operation_id": operation_id,
                "receipt_id": receipt_id,
                "summary": "The external effect outcome is unknown and requires reconciliation.",
                "reconciliation_status": "requires_attention",
            }
        )
    required_attention = []
    if unknown_effects:
        required_attention.append(
            {
                "attention_id": f"unknown-effect:{receipt_id}",
                "severity": "critical",
                "summary": "Reconcile the uncertain external effect before retrying.",
                "evidence_ids": [],
                "receipt_ids": [receipt_id],
            }
        )
    operation_summary = "Server-authorized Resident operation."
    receipt_body = getattr(receipt, "receipt", None)
    if isinstance(receipt_body, Mapping):
        structured = receipt_body.get("structured_result")
        operations = structured.get("operations") if isinstance(structured, Mapping) else None
        first_operation = operations[0] if isinstance(operations, list) and operations else None
        candidate = first_operation.get("summary") if isinstance(first_operation, Mapping) else None
        if isinstance(candidate, str) and 0 < len(candidate) <= 4_000:
            operation_summary = candidate
    return {
        "operation": {
            "operation_id": operation_id,
            "capability": capability_id,
            "status": operation_status,
            "summary": operation_summary,
        },
        "receipt": {
            "operation_id": operation_id,
            "receipt_id": receipt_id,
            "status": receipt_status,
            "summary": "Durable server operation receipt.",
        },
        "unknown_effects": unknown_effects,
        "required_attention": required_attention,
    }

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


_capability_rate_key = resident_task_token_rate_key


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
    except CorrectableCapabilityParameterError as exc:
        raise HTTPException(status_code=400, detail=exc.detail()) from exc
    except (StrategicAuthorityError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if spec.mode is not CapabilityMode.inspect:
        raise HTTPException(
            status_code=501,
            detail="mutation capability is proposal-only until a fenced adapter exists",
        )
    if body.capability_id not in INVOKABLE_CAPABILITY_IDS:
        raise HTTPException(
            status_code=501,
            detail="capability is proposal-only until a bounded adapter exists",
        )
    if body.capability_id == "pr.inspect":
        return await invoke_pr_inspection(
            session,
            binding=binding,
            parameters=parameters,
            inspector=inspect_project_pr,
        )
    if body.capability_id == "pr.diff.inspect":
        return await invoke_pr_diff_inspection(
            session,
            binding=binding,
            parameters=parameters,
            inspector=inspect_project_pr_diff,
        )
    if body.capability_id == "pr.checks.inspect":
        return await invoke_pr_checks_inspection(
            session,
            binding=binding,
            parameters=parameters,
            inspector=inspect_project_pr_checks,
        )
    if body.capability_id == "pipeline.inspect":
        return await invoke_project_inspection(
            session,
            binding=binding,
            capability_id="pipeline.inspect",
            inspector=inspect_project_pipeline,
        )
    if body.capability_id == "qualification.inspect":
        return await invoke_project_inspection(
            session,
            binding=binding,
            capability_id="qualification.inspect",
            inspector=inspect_project_qualification,
        )
    if spec.service not in {
        "resident_task_inspection",
        "admin_runners",
    }:
        raise HTTPException(
            status_code=501,
            detail="capability is declared but has no bounded adapter",
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
    fingerprint = request_fingerprint(body.capability_id, parameters)
    try:
        context_hash = str(
            (task.context or {}).get(TURN_CONTEXT_HASH_KEY) or ""
        )
        if not context_hash:
            raise HTTPException(
                status_code=409,
                detail="Resident turn context is not frozen for inspection",
            )
        request_registered = register_capability_request(
            task,
            fingerprint=fingerprint,
        )
        if request_registered:
            # Make the ledger write (and its updated_at onupdate) visible before
            # inspecting the turn Task itself, so returned revision evidence is
            # not stale immediately after commit.
            await session.flush()
        else:
            evidence_json = await canonical_inspection_replay(
                session,
                task=task,
                capability_id=body.capability_id,
                request_hash=fingerprint,
                context_hash=context_hash,
            )
            await session.commit()
            return {
                "capability_id": body.capability_id,
                "evidence": evidence_json,
            }
        if body.capability_id == "runner.inspect":
            evidence = await inspect_project_runner(
                session,
                project_id=task.project_id,
                runner_id=str(parameters["runner_id"]),
            )
        elif body.capability_id == "runner.list":
            evidence = await inspect_project_runners(
                session,
                project_id=task.project_id,
            )
        elif body.capability_id == "task.lineage.inspect":
            evidence = await inspect_project_task_lineage(
                session,
                project_id=task.project_id,
                task_id=uuid.UUID(parameters["task_id"]),
            )
        else:
            evidence = await inspect_project_task(
                session,
                project_id=task.project_id,
                task_id=uuid.UUID(parameters["task_id"]),
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
                context_hash=context_hash,
                request_hash=fingerprint,
                evidence_json=evidence_json,
            )
            if receipt is not None:
                session.add(receipt)
        await session.commit()
    except (ResidentTaskNotFound, ResidentRunnerNotFound) as exc:
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
    if body.capability_id not in INVOKABLE_CAPABILITY_IDS:
        raise HTTPException(
            status_code=501,
            detail="mutation capability is proposal-only until a fenced adapter exists",
        )
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
        raw_target = body.parameters.get("task_id")
        if raw_target is None:
            raise HTTPException(
                status_code=400,
                detail="task lifecycle mutation requires a task_id parameter",
            )
        target_task_id = uuid.UUID(str(raw_target))
        external_effect = build_task_lifecycle_effect(
            body.capability_id,
            project_id=binding.project_id,
            parameters=dict(body.parameters),
            expected_target_updated_at=(
                str(body.expected_revisions["target_task_updated_at"])
                if body.expected_revisions.get("target_task_updated_at") is not None
                else None
            ),
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
    elif spec.service == MANAGED_TASK_CREATE_SERVICE:
        callback = managed_task_create_callback(body.capability_id)
        operation_kind = managed_task_create_operation_kind(body.capability_id)
        if body.capability_id == "task.debug.create":
            target_task_id = uuid.UUID(str(body.parameters["task_id"]))
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
    # Validate decisive evidence before consuming a single-use operator
    # approval. A fabricated/stale evidence envelope must not be able to burn a
    # valid approval and then fail without performing its authorized effect.
    await _check_evidence_refs_against_receipts(session, binding, body.evidence)
    # Explicit-approval capabilities require a durable operator approval scoped to
    # this exact operation (the key is derived from the parameters, so an approval
    # granted for one operation cannot authorise a different one). Consumed here
    # in the request transaction — idempotent per operation_id — before any effect.
    # This durable-approval gate is the complete approval identity: it is
    # project-scoped, exact-parameter-bound, single-use, and TTL-bounded.
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
    fenced_payload = dict(body.parameters)
    if spec.service == MANAGED_TASK_CREATE_SERVICE:
        fenced_payload["_source_evidence"] = [
            item.model_dump(mode="json") for item in body.evidence
        ]
    fenced_request = FencedOperationRequest(
        project_id=binding.project_id,
        turn_task_id=binding.task_id,
        authority_generation=body.authority_generation,
        operation_id=str(body.operation_id),
        operation_kind=operation_kind,
        capability=body.capability_id,
        risk=spec.risk.value,
        expected_revisions=dict(body.expected_revisions),
        request_payload=fenced_payload,
        target_task_id=target_task_id,
    )
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
        "completion_projection": _completion_projection(
            receipt,
            capability_id=body.capability_id,
        ),
    }


__all__ = [
    "MAX_CAPABILITY_REQUESTS_PER_TURN",
    "ResidentCapabilityRequest",
    "router",
]
