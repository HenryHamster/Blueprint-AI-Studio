"""Canonical operator-facing compute execution and experiment status derivation."""

from __future__ import annotations

from typing import Any


_ACTIVE = {"QUEUED", "STARTING", "RUNNING", "POLLING"}


def derive_compute_outcome_status(
    job_status: Any,
    criteria_verdict: dict | None,
) -> dict[str, str]:
    """Keep workflow execution truth separate from scientific criterion truth."""
    execution_status = str(getattr(job_status, "value", job_status) or "UNKNOWN")
    if criteria_verdict is None:
        criterion_status = "NOT_DECLARED"
    else:
        criteria = criteria_verdict.get("criteria")
        criteria = criteria if isinstance(criteria, list) else []
        observation_states = {
            item.get("observation_status")
            for item in criteria
            if isinstance(item, dict)
        }
        if "INVALID" in observation_states:
            criterion_status = "INVALID"
        elif "UNMEASURED" in observation_states:
            criterion_status = "UNMEASURED"
        elif criteria_verdict.get("met") is True:
            criterion_status = "MET"
        else:
            criterion_status = "NOT_MET"

    if execution_status in _ACTIVE:
        experiment_status = execution_status
    elif execution_status == "SUCCEEDED":
        experiment_status = {
            "NOT_DECLARED": "EXECUTED_NO_CRITERION",
            "MET": "CRITERION_MET",
            "NOT_MET": "CRITERION_NOT_MET",
            "UNMEASURED": "CRITERION_UNMEASURED",
            "INVALID": "CRITERION_INVALID",
        }[criterion_status]
    elif execution_status == "CANCELLED":
        experiment_status = "EXECUTION_CANCELLED"
    elif execution_status == "TIMED_OUT":
        experiment_status = "EXECUTION_TIMED_OUT"
    else:
        experiment_status = "EXECUTION_FAILED"
    return {
        "execution_status": execution_status,
        "criterion_status": criterion_status,
        "experiment_status": experiment_status,
    }
