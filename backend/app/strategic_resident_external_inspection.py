"""Durable three-phase execution for Resident external inspections.

External inspection adapters reserve one receipt while the live Resident turn is
locked, release every database lock for network I/O, then re-lock and revalidate
the exact plan before settling canonical evidence.  The state machine is shared;
individual adapters only prepare a server-owned scope and perform one bounded
read.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timezone
from typing import Any, Protocol

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task, TaskStatus
from app.strategic_resident_agent_binding import (
    ResidentAgentAuthorityError,
    ResidentAgentBinding,
    ResidentAgentBindingError,
    ResidentAgentTaskNotFound,
    load_live_resident_agent_task_locked,
)
from app.strategic_resident_evidence_policy import (
    INSPECTION_CAPABILITY_KINDS,
    SUCCESSFUL_INSPECTION_STATUSES,
)
from app.strategic_resident_inspection_receipt import (
    TURN_CONTEXT_HASH_KEY,
    build_inspection_receipt,
    citable_inspection_evidence,
    inspection_operation_id,
)
from app.strategic_resident_models import StrategicOperationReceipt
from app.strategic_resident_inspection_runtime import (
    canonical_inspection_replay,
    register_capability_request,
    request_fingerprint,
    require_capability_request_registered,
)
from app.strategic_resident_task_inspection import MAX_EVIDENCE_RESPONSE_BYTES
from app.time_utils import _utcnow


# The wall-clock budget covers the complete adapter-owned perform phase, including
# credential discovery and every bounded external read. Reclaim remains later
# than that deadline so a legal in-flight attempt cannot be superseded merely
# because its sequential reads outlive one request timeout.
EXTERNAL_INSPECTION_TOTAL_TIMEOUT_SECONDS = 60
EXTERNAL_INSPECTION_LEASE_SECONDS = 90
if EXTERNAL_INSPECTION_LEASE_SECONDS <= EXTERNAL_INSPECTION_TOTAL_TIMEOUT_SECONDS:
    raise RuntimeError("external inspection lease must exceed its total timeout")
MAX_EXTERNAL_INSPECTION_PLAN_BYTES = 8_192
INSPECTION_PLAN_HASH_KEY = "_inspection_plan_hash"


class InspectionEvidence(Protocol):
    def model_dump(self, *, mode: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ExternalInspectionPlan:
    """Server-derived immutable inputs for one lock-free inspection attempt."""

    expected_revisions: dict[str, Any]
    reservation: dict[str, Any]
    external_scope: dict[str, Any]


PrepareExternalInspection = Callable[
    [AsyncSession, Task, ResidentAgentBinding, Mapping[str, Any]],
    Awaitable[ExternalInspectionPlan],
]
PerformExternalInspection = Callable[
    [ExternalInspectionPlan], Awaitable[InspectionEvidence]
]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _detached_plan(plan: ExternalInspectionPlan) -> ExternalInspectionPlan:
    """Detach adapter-owned dicts so phase-two code cannot mutate authority."""

    try:
        encoded = _canonical_json(
            {
                "expected_revisions": plan.expected_revisions,
                "reservation": plan.reservation,
                "external_scope": plan.external_scope,
            }
        )
        if len(encoded) > MAX_EXTERNAL_INSPECTION_PLAN_BYTES:
            raise HTTPException(
                status_code=409,
                detail="external inspection plan exceeds canonical size limit",
            )
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="external inspection plan is not canonical JSON",
        ) from exc
    expected = decoded.get("expected_revisions")
    reservation = decoded.get("reservation")
    scope = decoded.get("external_scope")
    if not all(isinstance(item, dict) for item in (expected, reservation, scope)):
        raise HTTPException(
            status_code=409,
            detail="external inspection plan is malformed",
        )
    return ExternalInspectionPlan(
        expected_revisions=expected,
        reservation=reservation,
        external_scope=scope,
    )


def _plan_identity(plan: ExternalInspectionPlan) -> bytes:
    return _canonical_json(
        {
            "expected_revisions": plan.expected_revisions,
            "reservation": plan.reservation,
            "external_scope": plan.external_scope,
        }
    )


def _bind_plan_to_context(
    prepared: ExternalInspectionPlan,
    *,
    context_hash: str,
) -> ExternalInspectionPlan:
    if {
        "context_hash",
        INSPECTION_PLAN_HASH_KEY,
    }.intersection(prepared.expected_revisions):
        raise HTTPException(
            status_code=409,
            detail="external inspection adapter cannot override executor lineage",
        )
    authority_plan = ExternalInspectionPlan(
        expected_revisions={
            "context_hash": context_hash,
            **prepared.expected_revisions,
        },
        reservation=prepared.reservation,
        external_scope=prepared.external_scope,
    )
    plan_hash = hashlib.sha256(_plan_identity(authority_plan)).hexdigest()
    return _detached_plan(
        ExternalInspectionPlan(
            expected_revisions={
                **authority_plan.expected_revisions,
                INSPECTION_PLAN_HASH_KEY: plan_hash,
            },
            reservation=authority_plan.reservation,
            external_scope=authority_plan.external_scope,
        )
    )


def _citable_bounded_evidence(
    evidence: InspectionEvidence,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        evidence_json = evidence.model_dump(mode="json")
        citable = citable_inspection_evidence(evidence_json)
        encoded = _canonical_json(citable)
    except (AttributeError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="external inspection evidence is not canonical JSON",
        ) from exc
    if len(encoded) > MAX_EVIDENCE_RESPONSE_BYTES:
        raise HTTPException(
            status_code=409,
            detail="external inspection evidence exceeds canonical size limit",
        )
    return evidence_json, citable


async def _load_turn_locked(
    session: AsyncSession,
    binding: ResidentAgentBinding,
) -> Task:
    try:
        return await load_live_resident_agent_task_locked(
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


def _receipt_matches(
    receipt: StrategicOperationReceipt,
    *,
    task: Task,
    capability_id: str,
    request_hash: str,
    expected_revisions: dict[str, Any],
) -> bool:
    return bool(
        receipt.project_id == task.project_id
        and receipt.turn_task_id == task.id
        and receipt.capability == capability_id
        and receipt.operation_kind == INSPECTION_CAPABILITY_KINDS.get(capability_id)
        and receipt.request_hash == request_hash
        and receipt.expected_revisions == expected_revisions
    )


def _pending_receipt(
    *,
    task: Task,
    capability_id: str,
    request_hash: str,
    plan: ExternalInspectionPlan,
    attempt_id: str,
) -> StrategicOperationReceipt:
    payload = task.first_payload if isinstance(task.first_payload, dict) else {}
    context = task.context if isinstance(task.context, dict) else {}
    raw_generation = payload.get("authority_generation")
    if raw_generation is None:
        raw_generation = context.get("_resident_authority_generation")
    try:
        generation = int(raw_generation)
    except (TypeError, ValueError):
        generation = 0
    operation_kind = INSPECTION_CAPABILITY_KINDS.get(capability_id)
    if generation < 1 or operation_kind is None:
        raise HTTPException(
            status_code=409,
            detail="external inspection reservation has invalid authority",
        )
    return StrategicOperationReceipt(
        project_id=task.project_id,
        turn_task_id=task.id,
        authority_generation=generation,
        operation_id=inspection_operation_id(task.id, request_hash),
        operation_kind=operation_kind,
        capability=capability_id,
        risk_level="low",
        request_hash=request_hash,
        expected_revisions=plan.expected_revisions,
        status="applying",
        receipt={
            "schema_version": 1,
            "reservation": {**plan.reservation, "attempt_id": attempt_id},
        },
    )


def _lease_remaining(receipt: StrategicOperationReceipt) -> int:
    observed = receipt.updated_at
    if observed.tzinfo is not None:
        observed = observed.astimezone(timezone.utc).replace(tzinfo=None)
    remaining = EXTERNAL_INSPECTION_LEASE_SECONDS - max(
        0.0, (_utcnow() - observed).total_seconds()
    )
    return max(0, int(remaining + 0.999))


def _attempt_id(receipt: StrategicOperationReceipt) -> str:
    body = receipt.receipt if isinstance(receipt.receipt, Mapping) else {}
    reservation = body.get("reservation")
    raw_attempt = (
        reservation.get("attempt_id") if isinstance(reservation, Mapping) else None
    )
    return str(raw_attempt or "")


def _claim_attempt(
    receipt: StrategicOperationReceipt,
    *,
    attempt_id: str,
) -> None:
    body = dict(receipt.receipt or {})
    reservation = dict(body.get("reservation") or {})
    reservation["attempt_id"] = attempt_id
    body["reservation"] = reservation
    receipt.status = "applying"
    receipt.receipt = body
    receipt.updated_at = _utcnow()


async def _release_failed_attempt(
    session: AsyncSession,
    *,
    binding: ResidentAgentBinding,
    capability_id: str,
    fingerprint: str,
    context_hash: str,
    expected_revisions: dict[str, Any],
    attempt_id: str,
) -> None:
    """Make one handled read failure immediately retryable for its live owner."""

    try:
        task = await load_live_resident_agent_task_locked(
            session,
            binding,
            allowed_statuses=frozenset({TaskStatus.IN_PROGRESS}),
        )
        if str((task.context or {}).get(TURN_CONTEXT_HASH_KEY) or "") != context_hash:
            raise ValueError("turn context changed")
        require_capability_request_registered(task, fingerprint=fingerprint)
        receipt = await session.scalar(
            select(StrategicOperationReceipt)
            .where(
                StrategicOperationReceipt.project_id == task.project_id,
                StrategicOperationReceipt.operation_id
                == inspection_operation_id(task.id, fingerprint),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            receipt is None
            or not _receipt_matches(
                receipt,
                task=task,
                capability_id=capability_id,
                request_hash=fingerprint,
                expected_revisions=expected_revisions,
            )
            or receipt.status != "applying"
            or _attempt_id(receipt) != attempt_id
        ):
            raise ValueError("attempt ownership changed")
        body = dict(receipt.receipt or {})
        reservation = dict(body.get("reservation") or {})
        reservation.pop("attempt_id", None)
        body["reservation"] = reservation
        receipt.status = "pending"
        receipt.receipt = body
        receipt.updated_at = _utcnow()
        session.add(receipt)
        await session.commit()
    except Exception:  # noqa: BLE001 - preserve the original external error
        await session.rollback()


async def execute_external_inspection(
    session: AsyncSession,
    *,
    binding: ResidentAgentBinding,
    capability_id: str,
    parameters: dict[str, Any],
    prepare_locked: PrepareExternalInspection,
    perform: PerformExternalInspection,
    authority_change_detail: str = "external inspection authority scope changed during I/O",
) -> dict[str, Any]:
    """Reserve, perform lock-free, and settle one canonical inspection."""

    task = await _load_turn_locked(session, binding)
    try:
        context_hash = str((task.context or {}).get(TURN_CONTEXT_HASH_KEY) or "")
        if not context_hash:
            raise HTTPException(
                status_code=409,
                detail="Resident turn context is not frozen for inspection",
            )
        prepared = _detached_plan(
            await prepare_locked(session, task, binding, parameters)
        )
        plan = _bind_plan_to_context(prepared, context_hash=context_hash)
        plan_identity = _plan_identity(plan)
        fingerprint = request_fingerprint(
            capability_id,
            parameters,
            authority_scope_hash=str(plan.expected_revisions[INSPECTION_PLAN_HASH_KEY]),
        )
        request_registered = register_capability_request(
            task,
            fingerprint=fingerprint,
        )
        attempt = ""
        existing_receipt = await session.scalar(
            select(StrategicOperationReceipt)
            .where(
                StrategicOperationReceipt.project_id == task.project_id,
                StrategicOperationReceipt.operation_id
                == inspection_operation_id(task.id, fingerprint),
            )
            .execution_options(populate_existing=True)
        )
        if request_registered:
            if existing_receipt is not None:
                raise HTTPException(
                    status_code=409,
                    detail="inspection receipt identity is already occupied",
                )
            attempt = uuid.uuid4().hex
            session.add(
                _pending_receipt(
                    task=task,
                    capability_id=capability_id,
                    request_hash=fingerprint,
                    plan=plan,
                    attempt_id=attempt,
                )
            )
        elif existing_receipt is None or not _receipt_matches(
            existing_receipt,
            task=task,
            capability_id=capability_id,
            request_hash=fingerprint,
            expected_revisions=plan.expected_revisions,
        ):
            raise HTTPException(
                status_code=409,
                detail="canonical external inspection reservation is unavailable or inconsistent",
            )
        elif existing_receipt.status in SUCCESSFUL_INSPECTION_STATUSES:
            evidence_json = await canonical_inspection_replay(
                session,
                task=task,
                capability_id=capability_id,
                request_hash=fingerprint,
                context_hash=context_hash,
            )
            await session.commit()
            return {"capability_id": capability_id, "evidence": evidence_json}
        elif existing_receipt.status == "applying":
            retry_after = _lease_remaining(existing_receipt)
            if retry_after > 0:
                raise HTTPException(
                    status_code=409,
                    detail="external inspection is already in progress",
                    headers={"Retry-After": str(retry_after)},
                )
            attempt = uuid.uuid4().hex
            _claim_attempt(existing_receipt, attempt_id=attempt)
            session.add(existing_receipt)
        elif existing_receipt.status == "pending":
            attempt = uuid.uuid4().hex
            _claim_attempt(existing_receipt, attempt_id=attempt)
            session.add(existing_receipt)
        else:
            raise HTTPException(
                status_code=409,
                detail="canonical external inspection reservation is not retryable",
            )
        if not attempt:
            raise HTTPException(
                status_code=409,
                detail="external inspection attempt ownership is unavailable",
            )
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    try:
        perform_plan = _detached_plan(plan)
        async with asyncio.timeout(EXTERNAL_INSPECTION_TOTAL_TIMEOUT_SECONDS):
            evidence = await perform(perform_plan)
    except TimeoutError as exc:
        await _release_failed_attempt(
            session,
            binding=binding,
            capability_id=capability_id,
            fingerprint=fingerprint,
            context_hash=context_hash,
            expected_revisions=plan.expected_revisions,
            attempt_id=attempt,
        )
        raise HTTPException(
            status_code=504,
            detail="external inspection exceeded its total time limit",
        ) from exc
    except Exception:
        await _release_failed_attempt(
            session,
            binding=binding,
            capability_id=capability_id,
            fingerprint=fingerprint,
            context_hash=context_hash,
            expected_revisions=plan.expected_revisions,
            attempt_id=attempt,
        )
        raise
    if _plan_identity(perform_plan) != plan_identity:
        await _release_failed_attempt(
            session,
            binding=binding,
            capability_id=capability_id,
            fingerprint=fingerprint,
            context_hash=context_hash,
            expected_revisions=plan.expected_revisions,
            attempt_id=attempt,
        )
        raise HTTPException(status_code=409, detail="external inspection plan mutated")
    try:
        evidence_json, citable_evidence = _citable_bounded_evidence(evidence)
    except HTTPException:
        await _release_failed_attempt(
            session,
            binding=binding,
            capability_id=capability_id,
            fingerprint=fingerprint,
            context_hash=context_hash,
            expected_revisions=plan.expected_revisions,
            attempt_id=attempt,
        )
        raise

    settled_task = await _load_turn_locked(session, binding)
    try:
        settled_context_hash = str(
            (settled_task.context or {}).get(TURN_CONTEXT_HASH_KEY) or ""
        )
        if settled_context_hash != context_hash:
            raise HTTPException(
                status_code=409,
                detail="Resident turn context changed during external inspection",
            )
        require_capability_request_registered(
            settled_task,
            fingerprint=fingerprint,
        )
        settled_prepared = _detached_plan(
            await prepare_locked(session, settled_task, binding, parameters)
        )
        settled_plan = _bind_plan_to_context(
            settled_prepared,
            context_hash=settled_context_hash,
        )
        if _plan_identity(settled_plan) != plan_identity:
            raise HTTPException(
                status_code=409,
                detail=authority_change_detail,
            )
        existing_receipt = await session.scalar(
            select(StrategicOperationReceipt)
            .where(
                StrategicOperationReceipt.project_id == settled_task.project_id,
                StrategicOperationReceipt.operation_id
                == inspection_operation_id(settled_task.id, fingerprint),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if existing_receipt is None or not _receipt_matches(
            existing_receipt,
            task=settled_task,
            capability_id=capability_id,
            request_hash=fingerprint,
            expected_revisions=plan.expected_revisions,
        ):
            raise HTTPException(
                status_code=409,
                detail="inspection receipt reservation changed during external inspection",
            )
        if existing_receipt.status in SUCCESSFUL_INSPECTION_STATUSES:
            evidence_json = await canonical_inspection_replay(
                session,
                task=settled_task,
                capability_id=capability_id,
                request_hash=fingerprint,
                context_hash=context_hash,
            )
            await session.commit()
            return {"capability_id": capability_id, "evidence": evidence_json}
        if existing_receipt.status != "applying":
            raise HTTPException(
                status_code=409,
                detail="external inspection receipt reservation is not settleable",
            )
        if _attempt_id(existing_receipt) != attempt:
            raise HTTPException(
                status_code=409,
                detail="external inspection attempt was superseded before settlement",
            )
        applied = build_inspection_receipt(
            capability_id=capability_id,
            turn_task=settled_task,
            context_hash=context_hash,
            request_hash=fingerprint,
            evidence_json=evidence_json,
        )
        if applied is None:
            raise HTTPException(
                status_code=409,
                detail="external inspection receipt could not be bound to the turn",
            )
        existing_receipt.status = "applied"
        existing_receipt.receipt = applied.receipt
        settled_at = _utcnow()
        existing_receipt.completed_at = settled_at
        existing_receipt.updated_at = settled_at
        session.add(existing_receipt)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return {
        "capability_id": capability_id,
        "evidence": citable_evidence,
    }


__all__ = [
    "EXTERNAL_INSPECTION_LEASE_SECONDS",
    "EXTERNAL_INSPECTION_TOTAL_TIMEOUT_SECONDS",
    "INSPECTION_PLAN_HASH_KEY",
    "MAX_EXTERNAL_INSPECTION_PLAN_BYTES",
    "ExternalInspectionPlan",
    "execute_external_inspection",
]
