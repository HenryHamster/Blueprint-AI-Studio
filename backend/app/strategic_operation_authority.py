"""Stable public API for Strategic Resident database mutation authority."""

from app.strategic_operation_authority_execution import (
    execute_fenced_db_operation,
)
from app.strategic_operation_authority_validation import (
    AuthorityBoundaryMutation,
    AuthorityGenerationMismatch,
    BoundedOperationResult,
    ExpectedRevisionMismatch,
    FencedMutationWriter,
    FencedOperationContext,
    FencedOperationRequest,
    FencedOperationUsageError,
    InactiveTurnAuthority,
    MutationCallback,
    MutationPlanTooLarge,
    OperationIdentityCollision,
    OperationPayloadTooLarge,
    OperationResultTooLarge,
    ResidentEngineOwnerMismatch,
    StrategicAuthorityError,
    UnsupportedRevisionKey,
)

__all__ = [
    "AuthorityBoundaryMutation",
    "AuthorityGenerationMismatch",
    "BoundedOperationResult",
    "ExpectedRevisionMismatch",
    "FencedMutationWriter",
    "FencedOperationContext",
    "FencedOperationRequest",
    "FencedOperationUsageError",
    "InactiveTurnAuthority",
    "MutationCallback",
    "MutationPlanTooLarge",
    "OperationIdentityCollision",
    "OperationPayloadTooLarge",
    "OperationResultTooLarge",
    "ResidentEngineOwnerMismatch",
    "StrategicAuthorityError",
    "UnsupportedRevisionKey",
    "execute_fenced_db_operation",
]
