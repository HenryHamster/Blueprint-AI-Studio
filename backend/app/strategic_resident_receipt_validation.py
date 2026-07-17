"""Complete-ledger receipt validation for Resident reconciliation."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StrategicOperationReceipt
from app.strategic_resident_reconciliation_validation import (
    ReconcileAcceptance,
    ReconciliationResultInvalid,
)
from app.strategic_resident_turn_results import StrategicResidentTurnResult


_UNSETTLED_RECEIPT_STATUSES = frozenset(
    {"pending", "applying", "reconciling", "unknown_effect"}
)
_OPERATION_STATUS_MATCHES = {
    "applying": frozenset({"pending", "applying", "reconciling", "unknown_effect"}),
    "applied": frozenset({"applied"}),
    "reconciled": frozenset({"reconciled"}),
    "failed": frozenset({"failed"}),
}


@dataclass(frozen=True)
class ReceiptValidation:
    unsettled_receipt_ids: tuple[str, ...]
    receipts: tuple[StrategicOperationReceipt, ...]


def _receipt_uuid(value: str, *, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise ReconciliationResultInvalid(f"{field} is not a receipt UUID") from None


def _unknown_effect_ids(receipt: StrategicOperationReceipt) -> set[str]:
    result: set[str] = set()
    for item in receipt.unknown_effects or []:
        if isinstance(item, Mapping):
            identity = item.get("effect_id", item.get("id"))
            if identity is not None:
                result.add(str(identity))
        elif item is not None:
            result.add(str(item))
    return result


async def validate_operation_receipts(
    session: AsyncSession,
    result: StrategicResidentTurnResult,
    request: ReconcileAcceptance,
    *,
    project_id: uuid.UUID,
    turn_task_id: uuid.UUID,
    authority_generation: int,
) -> ReceiptValidation:
    """Validate claims against the complete locked operation ledger for the turn."""

    del request  # Receipt admission depends on durable authority lineage, not accepts.
    claimed = [
        (_receipt_uuid(item.receipt_id, field="receipt_id"), item)
        for item in result.receipts
    ]
    unknown = [
        (_receipt_uuid(item.receipt_id, field="unknown_effect.receipt_id"), item)
        for item in result.unknown_effects
    ]
    claimed_ids = [identity for identity, _item in claimed]
    unknown_keys = [(identity, item.effect_id) for identity, item in unknown]
    if len(claimed_ids) != len(set(claimed_ids)):
        raise ReconciliationResultInvalid(
            "a StrategicOperationReceipt is claimed more than once"
        )
    if len(unknown_keys) != len(set(unknown_keys)):
        raise ReconciliationResultInvalid("an unknown operation effect is duplicated")
    rows = (
        (
            await session.execute(
                select(StrategicOperationReceipt)
                .where(
                    StrategicOperationReceipt.project_id == project_id,
                    StrategicOperationReceipt.turn_task_id == turn_task_id,
                    StrategicOperationReceipt.authority_generation
                    == int(authority_generation),
                )
                .order_by(StrategicOperationReceipt.id.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    by_id = {row.id: row for row in rows}
    referenced_ids = set(claimed_ids + [identity for identity, _item in unknown])
    missing_ids = referenced_ids - set(by_id)
    if missing_ids:
        raise ReconciliationResultInvalid(
            "operation result references a receipt outside the locked turn ledger: "
            f"{sorted(map(str, missing_ids))}"
        )
    for receipt_id, item in claimed:
        row = by_id.get(receipt_id)
        if (
            row is None
            or row.operation_id != item.operation_id
            or row.status != item.status
        ):
            raise ReconciliationResultInvalid(
                f"receipt claim does not match durable operation: {receipt_id}"
            )
    unsettled = {
        row.id: row for row in rows if row.status in _UNSETTLED_RECEIPT_STATUSES
    }
    unsettled_claim_ids = {
        receipt_id for receipt_id, _item in claimed if receipt_id in unsettled
    }
    if unsettled_claim_ids != set(unsettled):
        omitted = sorted(map(str, set(unsettled) - unsettled_claim_ids))
        raise ReconciliationResultInvalid(
            "operation result omitted unsettled durable receipts: " + ", ".join(omitted)
        )
    if set(claimed_ids) != set(by_id):
        omitted = sorted(map(str, set(by_id) - set(claimed_ids)))
        raise ReconciliationResultInvalid(
            "operation result must claim every durable turn receipt exactly once: "
            + ", ".join(omitted)
        )

    unknown_by_receipt: dict[uuid.UUID, set[str]] = {}
    for receipt_id, item in unknown:
        row = by_id.get(receipt_id)
        if (
            row is None
            or row.operation_id != item.operation_id
            or row.status not in _UNSETTLED_RECEIPT_STATUSES
        ):
            raise ReconciliationResultInvalid(
                f"unknown effect does not match an unsettled operation: {receipt_id}"
            )
        unknown_by_receipt.setdefault(receipt_id, set()).add(item.effect_id)

    for receipt_id, row in unsettled.items():
        durable_effect_ids = _unknown_effect_ids(row)
        declared_effect_ids = unknown_by_receipt.get(receipt_id, set())
        if not durable_effect_ids or declared_effect_ids != durable_effect_ids:
            raise ReconciliationResultInvalid(
                "unsettled durable receipt requires its exact unknown-effect "
                f"declaration: {receipt_id}"
            )

    attention_receipt_ids: set[uuid.UUID] = set()
    for item in result.required_attention:
        for raw_receipt_id in item.receipt_ids:
            receipt_id = _receipt_uuid(
                raw_receipt_id,
                field="required_attention.receipt_ids",
            )
            if receipt_id in attention_receipt_ids:
                raise ReconciliationResultInvalid(
                    "an unsettled receipt is referenced by attention more than once"
                )
            attention_receipt_ids.add(receipt_id)
    if attention_receipt_ids != set(unsettled):
        raise ReconciliationResultInvalid(
            "required attention must reference every and only unsettled receipt"
        )

    all_operation_ids = [item.operation_id for item in result.operations]
    if len(all_operation_ids) != len(set(all_operation_ids)):
        raise ReconciliationResultInvalid("an operation is claimed more than once")
    operations = {
        item.operation_id: item
        for item in result.operations
        if item.status != "proposed"
    }
    durable_by_operation = {row.operation_id: row for row in rows}
    if len(durable_by_operation) != len(rows):
        raise ReconciliationResultInvalid("a durable operation is claimed twice")
    if set(operations) != set(durable_by_operation):
        raise ReconciliationResultInvalid(
            "every non-proposed operation must map 1:1 to the durable turn ledger"
        )
    for operation_id, item in operations.items():
        row = durable_by_operation[operation_id]
        if (
            row.capability != item.capability
            or row.status not in _OPERATION_STATUS_MATCHES[item.status]
        ):
            raise ReconciliationResultInvalid(
                f"operation claim does not match durable capability/status: {operation_id}"
            )
    return ReceiptValidation(
        unsettled_receipt_ids=tuple(
            str(receipt_id) for receipt_id in sorted(unsettled, key=str)
        ),
        receipts=tuple(rows),
    )


__all__ = ["ReceiptValidation", "validate_operation_receipts"]
