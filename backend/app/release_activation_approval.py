"""Shared activation-approval authority for promoted release versions.

``ReleaseVersion.activation_requested_at`` is the single field the deploy agent
watches to execute an approved activation, and this module is the only place
that stamps it.  Two principals may reach it:

* a human admin, through ``POST /projects/{id}/approve-activation``; and
* the Resident strategic agent, through the ``approve_activation`` op on
  ``/api/agent/strategic``.

Both carry the SAME gates -- the preconditions live here once rather than being
restated per caller, so a second principal cannot arrive with a weaker set.
The callers differ only in recorded provenance, and that provenance is honest:
a Resident approval is stamped ``resident_approval`` with a
``strategic_task:<uuid>`` actor and can never present itself as a human
in-product approval.  :class:`ActivationApprover` is the only supported
constructor of that provenance, and neither of its constructors can produce the
other's mode, so the two authorities cannot be crossed by a caller mistake.

Authority is expressed as data (which approver a caller may construct), not as
a privileged code path: the Resident does not get its own gate list to drift
away from the human one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Dict

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Project, ReleaseVersion
from app.release_artifacts import (
    is_activation_rearmable_serving_status,
    version_serving_summary,
)
from app.time_utils import utc_iso

# Release/verification surface: this module owns the deploy-activation
# authority boundary, so it must stay small enough to review in one sitting.
_MODULE_SIZE_GOVERNANCE_CAP: int = 400

__all__ = [
    "APPROVAL_MODE_HUMAN",
    "APPROVAL_MODE_RESIDENT",
    "ActivationApprover",
    "approve_latest_promoted_activation",
]

# Durable audit values.  These are written into
# ``ReleaseVersion.last_qualification['activation_approval']['mode']`` and are
# read by operators reconstructing who authorised a deploy; treat them as a
# stable wire contract, not internal names.
APPROVAL_MODE_HUMAN = "in_product_approval"
APPROVAL_MODE_RESIDENT = "resident_approval"

# Actor prefix identifying a non-human principal.  An activation approved by
# the Resident is traceable to the exact strategic task that decided it.
RESIDENT_ACTOR_PREFIX = "strategic_task:"


@dataclass(frozen=True)
class ActivationApprover:
    """WHO approved an activation, and under which authority.

    Construct through :meth:`human` or :meth:`resident` only.  Each constructor
    hard-codes its own mode, so no caller-supplied value can turn a Resident
    approval into a human one.
    """

    mode: str
    actor: str

    @classmethod
    def human(cls, operator_id: object) -> "ActivationApprover":
        """A human admin approving in-product.

        ``operator_id`` is the admin's email or user id.  An empty id would
        produce an unattributable approval, so it is rejected rather than
        stamped.
        """
        actor = str(operator_id or "").strip()
        if not actor:
            raise ValueError(
                "human activation approval requires a non-empty operator id"
            )
        return cls(mode=APPROVAL_MODE_HUMAN, actor=actor)

    @classmethod
    def resident(cls, task_id: object) -> "ActivationApprover":
        """The Resident approving from a strategic task.

        The task id must parse as a UUID so every Resident approval resolves
        back to a real task row that recorded the decision.
        """
        try:
            parsed = uuid.UUID(str(task_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError(
                "resident activation approval requires a strategic task UUID"
            ) from exc
        return cls(
            mode=APPROVAL_MODE_RESIDENT,
            actor=f"{RESIDENT_ACTOR_PREFIX}{parsed}",
        )

    @property
    def is_resident(self) -> bool:
        return self.mode == APPROVAL_MODE_RESIDENT


async def approve_latest_promoted_activation(
    session: AsyncSession,
    project_id: uuid.UUID,
    approver: ActivationApprover,
    *,
    reason: str | None = None,
) -> Dict[str, Any]:
    """Approve activation of the latest promoted, still-unconfirmed version.

    Targets the latest ``promoted`` version under a ``with_for_update`` row
    lock, with no caller-supplied target: the approval always aims at the
    current promoted version, so it can never be aimed at a different or
    historical artifact.  The approval binds EXACTLY this version's immutable
    ``promoted_sha`` -- the activation-receipt path refuses any reported
    revision that differs, or a receipt once a newer promotion supersedes this
    version.

    Requires a promoted version whose aggregate serving status is either
    ``unconfirmed`` or ``confirmation_timed_out``.  The latter is a
    non-terminal report: its confirmation window elapsed without establishing
    serving, so the same explicit approval remains available to re-arm
    activation.  Already ``serving_confirmed`` / ``deploy_failed`` (or a
    never-armed ``serving_status`` NULL) -> 409.

    Idempotent: re-approving a still-unconfirmed version returns the existing
    approval without re-stamping, so a retrying caller cannot move the
    activation token.

    Commits before returning.  Every write here is a local UPDATE -- no network
    I/O runs under the row lock.  Lock order for the agent caller is
    Project -> Task -> ReleaseVersion; the human caller takes no exclusive
    Project lock, so the two paths cannot form a cycle.
    """
    from app.time_utils import _utcnow  # noqa: PLC0415

    project_row = await session.get(Project, project_id)
    if project_row is None:
        raise HTTPException(status_code=404, detail="Project not found.")

    version = (
        (
            await session.execute(
                select(ReleaseVersion)
                .options(selectinload(ReleaseVersion.deploy_artifacts))
                .where(
                    ReleaseVersion.project_id == project_id,
                    ReleaseVersion.status == "promoted",
                )
                .order_by(ReleaseVersion.version_number.desc())
                .limit(1)
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if version is None:
        raise HTTPException(
            status_code=404,
            detail="No promoted version awaiting activation for this project.",
        )

    aggregate, _ = version_serving_summary(version, project_row)
    aggregate_status = aggregate["serving_status"]

    if version.serving_status == "serving_confirmed" or aggregate_status == "serving_confirmed":
        raise HTTPException(
            status_code=409,
            detail=f"v{version.version_number} is already serving (serving_status={version.serving_status}); nothing left to approve.",
        )
    if version.serving_status == "deploy_failed" or aggregate_status == "deploy_failed":
        raise HTTPException(
            status_code=409,
            detail=f"v{version.version_number} deploy failed; clear the deploy failure before approving activation.",
        )
    if not is_activation_rearmable_serving_status(aggregate_status):
        raise HTTPException(
            status_code=409,
            detail=f"v{version.version_number} is not awaiting activation (serving_status={version.serving_status}).",
        )

    now = _utcnow()

    if (
        version.activation_requested_at is not None
        and aggregate_status != "confirmation_timed_out"
    ):
        # Idempotent re-approval while still unconfirmed -- return the existing
        # durable approval without re-stamping it. A timed-out approval is a
        # new execution attempt and must receive a fresh timestamp token.
        return _approval_result(project_id, version, already_approved=True)

    # Durable, immutable audit provenance (WHO approved and WHEN); the binding
    # to project / version / promoted SHA is inherent in this row.
    qual = dict(version.last_qualification or {})
    approval: Dict[str, Any] = {
        "mode": approver.mode,
        "operator": approver.actor,
        "approved_at": utc_iso(now),
        "approved_sha": version.promoted_sha,
    }
    # WHY, when the caller supplies it. The human in-product lever takes no
    # request body by design, so this stays absent there; a Resident approval
    # always carries one.
    if reason:
        approval["reason"] = reason
    qual["activation_approval"] = approval
    version.last_qualification = qual
    version.activation_requested_at = now
    version.activation_requested_by = approver.actor
    session.add(version)
    await session.commit()
    await session.refresh(version)
    return _approval_result(project_id, version, already_approved=False)


def _approval_result(
    project_id: uuid.UUID,
    version: ReleaseVersion,
    *,
    already_approved: bool,
) -> Dict[str, Any]:
    """Response shape shared by the fresh-approval and idempotent paths."""
    return {
        "version_number": version.version_number,
        "project_id": str(project_id),
        "promoted_sha": version.promoted_sha,
        "activation_requested_at": utc_iso(version.activation_requested_at),
        "activation_requested_by": version.activation_requested_by,
        "serving_status": version.serving_status,
        "already_approved": already_approved,
    }
