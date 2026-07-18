"""Adapters wiring bounded target-Task mutations to the fenced executor.

Unlike the Project-field adapters, these mutate one *non-turn target task*
(``request.target_task_id``) through ``FencedOperationContext.task_writer``,
CAS'd on ``target_task_updated_at``. The callback stays synchronous and only
records intent on the writer; the executor loads/locks/applies the task.
"""

from __future__ import annotations

from app.strategic_operation_authority_validation import (
    BoundedOperationResult,
    FencedOperationContext,
    FencedOperationUsageError,
    MutationCallback,
)


# Capabilities whose fenced adapter mutates one bounded target-task field.
TASK_MUTATION_SERVICE = "task_lifecycle"

# capability_id -> the receipt ``operation_kind`` recorded for the mutation.
_OPERATION_KINDS = {
    "task.reprioritize": "reprioritize_task",
}


def _reprioritize(context: FencedOperationContext) -> BoundedOperationResult:
    if context.task_writer is None:  # pragma: no cover - executor guarantees this
        raise FencedOperationUsageError("target task writer is unavailable")
    # ``priority`` is already range-validated (0..100) by the invocation envelope;
    # the writer re-checks the bound.
    context.task_writer.set_priority(int(context.request_payload["priority"]))
    return BoundedOperationResult(reply="task reprioritized")


_CALLBACKS: dict[str, MutationCallback] = {
    "task.reprioritize": _reprioritize,
}


def task_mutation_callback(capability_id: str) -> MutationCallback | None:
    """Return the fenced callback for a target-task capability, or None."""

    return _CALLBACKS.get(capability_id)


def task_mutation_operation_kind(capability_id: str) -> str | None:
    """Return the receipt operation_kind for a target-task capability, or None."""

    return _OPERATION_KINDS.get(capability_id)


__all__ = [
    "TASK_MUTATION_SERVICE",
    "task_mutation_callback",
    "task_mutation_operation_kind",
]
