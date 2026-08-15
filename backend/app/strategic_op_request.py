"""Validated request boundary for the generated strategic mutation helper."""

import uuid
from typing import Any

from pydantic import BaseModel, model_validator


class StrategicOp(BaseModel):
    task_id: uuid.UUID
    resident_incarnation_id: uuid.UUID | None = None
    packet_id: uuid.UUID | None = None
    workday_claim_id: uuid.UUID | None = None
    workday_operation_id: uuid.UUID | None = None
    autonomous_turn_id: uuid.UUID | None = None
    autonomous_turn_lease_id: uuid.UUID | None = None
    autonomous_handoff_idempotency_key: uuid.UUID | None = None
    op: str
    data: Any = {}

    @model_validator(mode="after")
    def validate_workday_provenance(self) -> "StrategicOp":
        supplied = (
            self.packet_id is not None,
            self.workday_claim_id is not None,
            self.workday_operation_id is not None,
        )
        if any(supplied) and not all(supplied):
            raise ValueError(
                "packet_id, workday_claim_id, and workday_operation_id must be "
                "supplied together"
            )
        autonomous = (
            self.autonomous_turn_id is not None,
            self.autonomous_turn_lease_id is not None,
            self.autonomous_handoff_idempotency_key is not None,
        )
        if any(autonomous) and not all(autonomous):
            raise ValueError(
                "autonomous_turn_id, autonomous_turn_lease_id, and "
                "autonomous_handoff_idempotency_key must be supplied together"
            )
        if any(supplied) and any(autonomous):
            raise ValueError("consultation and autonomous provenance cannot be combined")
        return self


__all__ = ["StrategicOp"]
