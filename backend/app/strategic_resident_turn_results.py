"""Typed, bounded result contract for one Strategic Resident turn."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.strategic_operation_result_bounds import bounded_result


Identifier = str
EvidenceKind = Literal[
    "task",
    "task_log",
    "pr",
    "pr_diff",
    "pr_check",
    "ci_run",
    "deployment",
    "artifact",
    "operator_message",
    "other",
]
ImplicationKind = Literal[
    "strategy_change",
    "abandon_work",
    "merge",
    "deploy",
    "other",
]
MemoryLayer = Literal["charter", "working_strategy"]


class _StrictResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResidentFact(_StrictResultModel):
    fact_id: Identifier = Field(min_length=1, max_length=120)
    statement: str = Field(min_length=1, max_length=4_000)
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence_ids: list[Identifier] = Field(default_factory=list, max_length=20)


class ResidentEvidence(_StrictResultModel):
    evidence_id: Identifier = Field(min_length=1, max_length=120)
    kind: EvidenceKind
    source_id: str = Field(min_length=1, max_length=512)
    revision: str | None = Field(default=None, max_length=512)
    checksum: str | None = Field(default=None, max_length=256)
    summary: str = Field(min_length=1, max_length=4_000)


class ResidentEvidenceInspection(_StrictResultModel):
    evidence_id: Identifier = Field(min_length=1, max_length=120)
    kind: EvidenceKind
    source_id: str = Field(min_length=1, max_length=512)
    revision: str = Field(min_length=1, max_length=512)
    checksum: str = Field(min_length=1, max_length=256)
    inspected_at: str = Field(min_length=1, max_length=128)
    decisive: bool = True


class ResidentOperation(_StrictResultModel):
    operation_id: Identifier = Field(min_length=1, max_length=200)
    capability: str = Field(min_length=1, max_length=200)
    status: Literal[
        "proposed",
        "applying",
        "applied",
        "reconciled",
        "failed",
    ]
    summary: str = Field(min_length=1, max_length=4_000)


class ResidentReceipt(_StrictResultModel):
    operation_id: Identifier = Field(min_length=1, max_length=200)
    receipt_id: str = Field(min_length=1, max_length=200)
    status: Literal[
        "applying",
        "applied",
        "reconciling",
        "reconciled",
        "failed",
        "unknown_effect",
    ]
    summary: str = Field(min_length=1, max_length=4_000)


class ResidentUnknownEffect(_StrictResultModel):
    effect_id: Identifier = Field(min_length=1, max_length=120)
    operation_id: Identifier = Field(min_length=1, max_length=200)
    receipt_id: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=4_000)
    reconciliation_status: Literal[
        "unknown",
        "reconciled",
        "requires_attention",
    ] = "unknown"


class ResidentStrategicImplication(_StrictResultModel):
    implication_id: Identifier = Field(min_length=1, max_length=120)
    kind: ImplicationKind
    summary: str = Field(min_length=1, max_length=4_000)
    evidence_ids: list[Identifier] = Field(default_factory=list, max_length=20)


class ResidentMemoryProposal(_StrictResultModel):
    proposal_id: Identifier = Field(min_length=1, max_length=120)
    layer: MemoryLayer
    updates: dict[str, Any]
    rationale: str = Field(min_length=1, max_length=4_000)
    evidence_ids: list[Identifier] = Field(default_factory=list, max_length=20)

    @field_validator("updates")
    @classmethod
    def _updates_are_nonempty(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("memory proposal updates cannot be empty")
        if "open_loops" in value:
            raise ValueError("open_loops are reconciled through follow_up")
        return value


class ResidentOpenLoop(_StrictResultModel):
    loop_id: Identifier = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=4_000)
    owner: str | None = Field(default=None, max_length=200)
    due_at: str | None = Field(default=None, max_length=128)
    evidence_ids: list[Identifier] = Field(default_factory=list, max_length=20)


class ResidentFollowUp(_StrictResultModel):
    summary: str | None = Field(default=None, max_length=4_000)
    open_loops: list[ResidentOpenLoop] = Field(default_factory=list, max_length=50)
    next_wakeup_at: str | None = Field(default=None, max_length=128)


class ResidentRequiredAttention(_StrictResultModel):
    attention_id: Identifier = Field(min_length=1, max_length=120)
    severity: Literal["info", "warning", "critical"]
    summary: str = Field(min_length=1, max_length=4_000)
    evidence_ids: list[Identifier] = Field(default_factory=list, max_length=20)
    receipt_ids: list[str] = Field(default_factory=list, max_length=50)


class StrategicResidentTurnResult(_StrictResultModel):
    reply: str | None = Field(default=None, max_length=4_000)
    facts: list[ResidentFact] = Field(default_factory=list, max_length=50)
    evidence: list[ResidentEvidence] = Field(default_factory=list, max_length=50)
    operations: list[ResidentOperation] = Field(default_factory=list, max_length=50)
    receipts: list[ResidentReceipt] = Field(default_factory=list, max_length=50)
    unknown_effects: list[ResidentUnknownEffect] = Field(
        default_factory=list,
        max_length=50,
    )
    strategic_implications: list[ResidentStrategicImplication] = Field(
        default_factory=list,
        max_length=50,
    )
    proposed_memory: list[ResidentMemoryProposal] = Field(
        default_factory=list,
        max_length=50,
    )
    follow_up: ResidentFollowUp | None = None
    required_attention: list[ResidentRequiredAttention] = Field(
        default_factory=list,
        max_length=50,
    )


def normalize_strategic_resident_turn_result(
    value: StrategicResidentTurnResult | Mapping[str, Any],
) -> StrategicResidentTurnResult:
    """Apply the shared operation-result bounds before strict typed validation."""

    raw = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return StrategicResidentTurnResult.model_validate(bounded_result(raw))


__all__ = [
    "ResidentEvidence",
    "ResidentEvidenceInspection",
    "ResidentMemoryProposal",
    "ResidentOpenLoop",
    "ResidentStrategicImplication",
    "StrategicResidentTurnResult",
    "normalize_strategic_resident_turn_result",
]
