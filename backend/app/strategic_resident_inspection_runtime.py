"""Shared request-budget and canonical-replay runtime for Resident inspections."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task
from app.strategic_resident_evidence_policy import (
    INSPECTION_CAPABILITY_KINDS,
    SUCCESSFUL_INSPECTION_STATUSES,
)
from app.strategic_resident_inspection_receipt import inspection_operation_id
from app.strategic_resident_models import StrategicOperationReceipt
from app.strategic_resident_task_inspection import MAX_EVIDENCE_RESPONSE_BYTES


MAX_CAPABILITY_REQUESTS_PER_TURN = 8
CAPABILITY_REQUEST_LEDGER_CONTEXT_KEY = "_strategic_resident_capability_requests"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def request_fingerprint(
    capability_id: str,
    parameters: dict[str, Any],
    *,
    authority_scope_hash: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "capability_id": capability_id,
        "parameters": parameters,
    }
    if authority_scope_hash is not None:
        payload["authority_scope_hash"] = authority_scope_hash
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def register_capability_request(task: Any, *, fingerprint: str) -> bool:
    """Consume one durable unique-call slot under the locked turn Task."""

    context = task.context if isinstance(task.context, dict) else {}
    raw_ledger = context.get(CAPABILITY_REQUEST_LEDGER_CONTEXT_KEY, [])
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
        CAPABILITY_REQUEST_LEDGER_CONTEXT_KEY: [*ledger, fingerprint],
    }
    return True


def require_capability_request_registered(task: Any, *, fingerprint: str) -> None:
    """Fail closed unless phase one durably registered this exact request."""

    context = task.context if isinstance(task.context, dict) else {}
    raw_ledger = context.get(CAPABILITY_REQUEST_LEDGER_CONTEXT_KEY, [])
    if (
        not isinstance(raw_ledger, list)
        or fingerprint not in raw_ledger
        or any(
            not isinstance(item, str)
            or len(item) != 64
            or any(character not in "0123456789abcdef" for character in item)
            for item in raw_ledger
        )
    ):
        raise HTTPException(
            status_code=409,
            detail="capability request registration is missing or invalid",
        )


async def canonical_inspection_replay(
    session: AsyncSession,
    *,
    task: Task,
    capability_id: str,
    request_hash: str,
    context_hash: str,
) -> dict[str, Any]:
    """Load the one bounded receipt-backed snapshot for an idempotent retry."""

    receipt = (
        await session.execute(
            select(StrategicOperationReceipt).where(
                StrategicOperationReceipt.project_id == task.project_id,
                StrategicOperationReceipt.turn_task_id == task.id,
                StrategicOperationReceipt.operation_id
                == inspection_operation_id(task.id, request_hash),
            )
        )
    ).scalar_one_or_none()
    if (
        receipt is None
        or not context_hash
        or receipt.capability != capability_id
        or receipt.operation_kind != INSPECTION_CAPABILITY_KINDS.get(capability_id)
        or receipt.request_hash != request_hash
        or receipt.status not in SUCCESSFUL_INSPECTION_STATUSES
        or str((receipt.expected_revisions or {}).get("context_hash") or "")
        != context_hash
    ):
        raise HTTPException(
            status_code=409,
            detail="canonical inspection receipt is unavailable or inconsistent",
        )
    body = receipt.receipt if isinstance(receipt.receipt, Mapping) else {}
    structured = body.get("structured_result")
    evidence = structured.get("evidence") if isinstance(structured, Mapping) else None
    if (
        not isinstance(evidence, list)
        or len(evidence) != 1
        or not isinstance(evidence[0], Mapping)
    ):
        raise HTTPException(
            status_code=409,
            detail="canonical inspection receipt evidence is invalid",
        )
    canonical = dict(evidence[0])
    try:
        if len(_canonical_json(canonical)) > MAX_EVIDENCE_RESPONSE_BYTES:
            raise ValueError("oversized evidence")
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="canonical inspection receipt evidence exceeds its bound",
        ) from exc
    return canonical


__all__ = [
    "CAPABILITY_REQUEST_LEDGER_CONTEXT_KEY",
    "MAX_CAPABILITY_REQUESTS_PER_TURN",
    "canonical_inspection_replay",
    "register_capability_request",
    "request_fingerprint",
    "require_capability_request_registered",
]
