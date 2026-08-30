"""Pure evidence-contract gradeability rules for prepromotion rehearsal."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_MODULE_SIZE_GOVERNANCE_CAP: int = 150

PREPROMOTION_REHEARSAL_CONTRACT_TYPE = "prepromotion_ci_build_smoke"

_PREPROMOTION_GRADEABILITY_PHASES = {
    "qualification_setup": 10,
    "qualification_evidence": 20,
    "promotion_gate": 30,
}


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


def validate_prepromotion_gradeability(
    active_categories: list[Mapping[str, Any]],
    *,
    contracts: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Reject promotion criteria whose evidence producer is not pre-gradeable."""

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


def rubric_requires_rehearsal_contract(criteria: list[Mapping[str, Any]]) -> bool:
    return any(
        normalize_rehearsal_evidence_contract(item.get("evidence_contract")) is not None
        for item in criteria
    )
