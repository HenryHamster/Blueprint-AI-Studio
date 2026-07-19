"""Adapters wiring DB-only strategic-memory mutations to the fenced executor.

These are the first mutation capabilities lifted off their HTTP 501 stub. Each
adapter is a *synchronous, database-only* callback that the fenced executor runs
inside its owned transaction: it may only touch bounded Project fields through
``FencedOperationContext.writer`` and must return a ``BoundedOperationResult``.
Anything with an external side effect (PR/deploy) or a Task-table write stays
stubbed until its own fenced adapter exists.
"""

from __future__ import annotations

from app.strategic_operation_authority_validation import (
    BoundedOperationResult,
    FencedOperationContext,
    MutationCallback,
)


# Capabilities whose fenced adapter mutates only a bounded Project field via
# ``FencedMutationWriter``. The router dispatches exactly this service here.
PROJECT_FIELD_MUTATION_SERVICE = "resident_strategy_memory"

# capability_id -> the receipt ``operation_kind`` recorded for the mutation.
_OPERATION_KINDS = {
    "strategy.running_update.set": "set_running_update",
}


def _set_running_update(context: FencedOperationContext) -> BoundedOperationResult:
    # The parameter is already name/type/size-validated by the capability
    # invocation envelope; the writer re-checks the field byte budget.
    context.writer.set_running_update(str(context.request_payload["running_update"]))
    return BoundedOperationResult(reply="running update recorded")


_CALLBACKS: dict[str, MutationCallback] = {
    "strategy.running_update.set": _set_running_update,
}


def project_field_mutation_callback(capability_id: str) -> MutationCallback | None:
    """Return the fenced callback for a project-field capability, or None."""

    return _CALLBACKS.get(capability_id)


def project_field_mutation_operation_kind(capability_id: str) -> str | None:
    """Return the receipt operation_kind for a project-field capability, or None."""

    return _OPERATION_KINDS.get(capability_id)


__all__ = [
    "PROJECT_FIELD_MUTATION_SERVICE",
    "project_field_mutation_callback",
    "project_field_mutation_operation_kind",
]
