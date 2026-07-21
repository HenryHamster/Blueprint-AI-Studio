"""Attempt-scoped context handling for failed Strategic Resident turns."""

from __future__ import annotations

from typing import Any

from app.max_plan_pool import clear_task_llm_runtime_context
from app.strategic_resident_agent_binding import (
    RESIDENT_CLAIM_ATTEMPT_CONTEXT_KEY,
    RESIDENT_CLAIM_NONCE_CONTEXT_KEY,
)
from app.strategic_resident_inspection_runtime import (
    CAPABILITY_REQUEST_LEDGER_CONTEXT_KEY,
)
from app.strategic_resident_slot_release import clear_resident_turn_slot_release


def resident_failure_retry_context(context: object) -> dict[str, Any]:
    """Clone durable Resident context without prior-attempt failure evidence."""
    cleaned = clear_task_llm_runtime_context(context)
    for key in (
        RESIDENT_CLAIM_NONCE_CONTEXT_KEY,
        RESIDENT_CLAIM_ATTEMPT_CONTEXT_KEY,
        CAPABILITY_REQUEST_LEDGER_CONTEXT_KEY,
        "_resident_failure",
        "agent_error_report",
    ):
        cleaned.pop(key, None)
    clear_resident_turn_slot_release(cleaned)
    return cleaned


__all__ = ["resident_failure_retry_context"]
