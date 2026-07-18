"""Operator-approval verification for explicit-approval Resident operations.

Explicit-approval capabilities (pr.merge, deployment.*) require a durable
operator approval scoped to the EXACT operation. The ``approval_key`` is derived
from the invocation's own parameters, so the Resident cannot present an approval
granted for one operation and execute a different one. Verification is single-use
and idempotent per fenced ``operation_id``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StrategicOperationApproval
from app.strategic_operation_authority_validation import StrategicAuthorityError
from app.time_utils import _utcnow


# Per-capability derivation of the approval key from the operation parameters.
# Only capabilities listed here can be operator-approved; the derived key binds
# the approval to the exact, safety-relevant parameters of the operation.
_APPROVAL_KEY_PARAMETERS: dict[str, tuple[str, ...]] = {
    "pr.merge": ("pr_number", "expected_head_sha"),
    "deployment.deploy": ("revision",),
    "deployment.redeploy": ("revision",),
    "deployment.rollback": ("revision",),
}

MAX_APPROVAL_TTL_SECONDS = 24 * 60 * 60


class OperatorApprovalRequired(StrategicAuthorityError):
    code = "operator_approval_required"


def capability_requires_operator_approval(capability_id: str) -> bool:
    return capability_id in _APPROVAL_KEY_PARAMETERS


def derive_approval_key(capability_id: str, parameters: dict) -> str:
    """Return the deterministic approval key an operator must grant.

    The key encodes the capability plus its safety-relevant parameters, so an
    approval is bound to one exact operation.
    """

    fields = _APPROVAL_KEY_PARAMETERS.get(capability_id)
    if fields is None:
        raise OperatorApprovalRequired(
            f"{capability_id} is not an operator-approvable capability"
        )
    parts = [capability_id, *(f"{field}={parameters.get(field)}" for field in fields)]
    return ";".join(parts)


async def grant_operation_approval(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    capability: str,
    approval_key: str,
    granted_by: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> StrategicOperationApproval:
    """Record an operator approval (producer, called from the admin surface)."""

    if not capability_requires_operator_approval(capability):
        raise OperatorApprovalRequired(
            f"{capability} does not require explicit operator approval"
        )
    ttl = max(1, min(int(ttl_seconds), MAX_APPROVAL_TTL_SECONDS))
    issued = now or _utcnow()
    approval = StrategicOperationApproval(
        project_id=project_id,
        capability=capability,
        approval_key=approval_key,
        granted_by=granted_by,
        granted_at=issued,
        expires_at=issued + _seconds(ttl),
    )
    session.add(approval)
    await session.flush()
    return approval


def _seconds(count: int):
    from datetime import timedelta

    return timedelta(seconds=count)


async def verify_and_consume_operation_approval(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    capability: str,
    approval_key: str,
    operation_id: str,
    now: datetime | None = None,
) -> StrategicOperationApproval:
    """Consume one matching unexpired approval, or raise OperatorApprovalRequired.

    Idempotent per ``operation_id``: an approval already consumed by THIS
    operation is re-accepted (so a retry of the same fenced operation does not
    need a fresh approval), while one consumed by a different operation is not.
    """

    observed = now or _utcnow()
    rows = (
        (
            await session.execute(
                select(StrategicOperationApproval)
                .where(
                    StrategicOperationApproval.project_id == project_id,
                    StrategicOperationApproval.capability == capability,
                    StrategicOperationApproval.approval_key == approval_key,
                )
                .order_by(StrategicOperationApproval.granted_at.desc())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    # Idempotent retry: this exact operation already consumed a matching approval.
    for row in rows:
        if row.consumed_operation_id == operation_id:
            return row
    # Otherwise consume the most recent unexpired, unconsumed approval.
    for row in rows:
        if row.consumed_at is None and row.expires_at > observed:
            row.consumed_at = observed
            row.consumed_operation_id = operation_id
            session.add(row)
            return row
    raise OperatorApprovalRequired(
        f"no valid operator approval for {capability} ({approval_key})"
    )


__all__ = [
    "MAX_APPROVAL_TTL_SECONDS",
    "OperatorApprovalRequired",
    "capability_requires_operator_approval",
    "derive_approval_key",
    "grant_operation_approval",
    "verify_and_consume_operation_approval",
]
