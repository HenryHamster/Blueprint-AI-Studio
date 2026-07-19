"""Two-phase executor for Resident operations with an external side effect.

Unlike ``execute_fenced_db_operation`` (DB-only, one owned transaction), an
external effect (create a GitHub PR, trigger a deploy) performs async I/O that
cannot happen inside the fenced transaction. This executor splits the work:

1. **Intent** — a fenced transaction validates authority and commits an
   ``applying`` receipt whose ``unknown_effects`` is pre-seeded (so a crash here
   leaves a durably-unsettled receipt the reconcile/failure machinery surfaces).
2. **Perform** — outside any transaction/lock, run the *idempotent* external
   effect and capture its result (or the exception).
3. **Settle** — a fenced transaction re-locks the receipt and finalizes it to
   ``reconciled`` (success) or ``failed`` (error).

Single-owner perform: between intent and perform, the receipt is claimed by a
compare-and-set ``applying -> reconciling``. Exactly one worker wins the claim
and performs; a concurrent invocation (or a retry racing an in-flight perform)
that finds the receipt already ``reconciling`` returns it without performing, so
two workers never create two side effects. The ``(project_id, operation_id)``
uniqueness also stops two concurrent *new* invocations from both inserting.

Crash-safety: a crash after intent but before the claim leaves an ``applying``
receipt that a retry re-claims and performs (the effect is idempotent). A crash
after the claim leaves a ``reconciling`` receipt that is NOT auto-re-performed —
the reconcile/failure machinery surfaces it for settlement, the conservative
choice for a side effect that may already have happened. Residual (accepted):
exactly-once still ultimately relies on the external primitive's own idempotency
(``create_pull_request`` dedups by ``(head, base)``); a retry with a *fresh*
``operation_id`` bypasses the receipt dedup entirely.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import StrategicOperationReceipt
from app.strategic_operation_authority_validation import (
    RESIDENT_TURN_TASK_TYPE,
    AuthorityBoundaryMutation,
    FencedOperationRequest,
    InactiveTurnAuthority,
    OperationIdentityCollision,
    assert_locked_turn_mutation_authority,
    _REQUIRED_AUTHORITY_REVISIONS,
    _observed_revisions,
    _request_fingerprint,
    _require_expected_revision_keys,
    _validate_expected_revisions,
    _validate_frozen_turn_lineage,
    _validated_request_payload,
)
from app.time_utils import _utcnow


# The external effect: idempotent async I/O returning a JSON-able result dict.
ExternalEffect = Callable[[AsyncSession], Awaitable[dict[str, Any]]]

_UNSETTLED = "applying"
# Single-owner "perform in flight" marker (a valid unsettled status). A worker
# that finds the receipt already in this state does not re-perform.
_CLAIMED = "reconciling"
_TERMINAL = frozenset({"applied", "reconciled", "failed"})
_REQUIRES_RECONCILIATION = frozenset({_CLAIMED, "unknown_effect"})


class ExternalEffectNotPerformed(RuntimeError):
    """A proven pre-effect failure that is safe to settle as ``failed``."""

# Redact anything that looks like a GitHub/OAuth token from a settled error body
# so a client-exception string can never persist a secret in the receipt JSONB.
_SECRET_RE = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|Bearer\s+[A-Za-z0-9._\-]+|ey[A-Za-z0-9._\-]{20,})"
)


def _scrub_effect_error(exc: Exception) -> str:
    return _SECRET_RE.sub("[redacted]", str(exc))[:900]


def _factory(
    session_factory: async_sessionmaker[AsyncSession] | AsyncSession,
) -> async_sessionmaker[AsyncSession]:
    if isinstance(session_factory, AsyncSession):
        return async_sessionmaker(
            bind=session_factory.bind, expire_on_commit=False, autoflush=False
        )
    return session_factory


async def execute_external_effect_operation(
    session_factory: async_sessionmaker[AsyncSession] | AsyncSession,
    request: FencedOperationRequest,
    *,
    perform: ExternalEffect,
    effect_kind: str,
) -> StrategicOperationReceipt:
    """Run one authority-fenced operation whose effect is an external side effect."""

    request_payload = _validated_request_payload(request.request_payload)
    fingerprint = _request_fingerprint(request, request_payload=request_payload)
    factory = _factory(session_factory)
    effect_id = f"{effect_kind}:{request.operation_id}"

    from app.models import StrategicResident

    # --- Phase 1: adopt terminal / resume applying / write new applying intent ---
    async with factory() as session, session.begin():
        existing = (
            await session.execute(
                select(StrategicOperationReceipt)
                .where(
                    StrategicOperationReceipt.project_id == request.project_id,
                    StrategicOperationReceipt.operation_id
                    == str(request.operation_id).strip(),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.request_hash != fingerprint:
                raise OperationIdentityCollision(
                    "operation_id already belongs to a different request",
                    operation_id=request.operation_id,
                )
            if existing.status in _TERMINAL:
                return existing
            if existing.status in _REQUIRES_RECONCILIATION:
                # Another worker owns the in-flight perform (or crashed mid-perform);
                # surface the unsettled receipt rather than re-performing.
                return existing
            # Durably ``applying`` (crash before the perform was claimed): fall
            # through to claim, perform the idempotent effect, and settle it.
            receipt_id = existing.id
        else:
            _require_expected_revision_keys(
                request.expected_revisions,
                _REQUIRED_AUTHORITY_REVISIONS,
                reason="external-effect operation",
            )
            from app.models import Project, Task

            # Same lock order as execute_fenced_db_operation: Project (key-share)
            # -> Resident (exclusive) -> turn Task (exclusive). The Resident lock
            # protects the authority generation / checkpoint pointer read below.
            project_row = (
                await session.execute(
                    select(Project)
                    .where(Project.id == request.project_id)
                    .with_for_update(key_share=True)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if project_row is None:
                raise InactiveTurnAuthority("project not found")
            resident = (
                await session.execute(
                    select(StrategicResident)
                    .where(StrategicResident.project_id == request.project_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if resident is None:
                raise InactiveTurnAuthority("Strategic Resident not found")
            turn_task = (
                await session.execute(
                    select(Task)
                    .where(Task.id == request.turn_task_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            observed_generation = assert_locked_turn_mutation_authority(
                resident, turn_task, request
            )
            observed = await _observed_revisions(
                session, project_row, resident, turn_task, request.expected_revisions
            )
            if request.target_task_id is not None:
                if request.target_task_id == request.turn_task_id:
                    raise AuthorityBoundaryMutation(
                        "operation cannot target its own Resident turn task"
                    )
                _require_expected_revision_keys(
                    request.expected_revisions,
                    {"target_task_updated_at"},
                    reason="target-task external operation",
                )
                target_task = (
                    await session.execute(
                        select(Task)
                        .where(
                            Task.id == request.target_task_id,
                            Task.project_id == request.project_id,
                        )
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if target_task is None:
                    raise InactiveTurnAuthority("target task not found in project")
                if (
                    str((target_task.first_payload or {}).get("type") or "")
                    == RESIDENT_TURN_TASK_TYPE
                ):
                    raise AuthorityBoundaryMutation(
                        "operation cannot target a Resident turn task"
                    )
                observed["target_task_updated_at"] = (
                    target_task.updated_at.isoformat()
                    if target_task.updated_at
                    else None
                )
            _validate_frozen_turn_lineage(
                turn_task,
                project_id=request.project_id,
                engine_owner=str(resident.engine_owner),
                authority_generation=observed_generation,
                checkpoint_version=observed["resident_checkpoint_version"],
                checkpoint_hash=observed["resident_checkpoint_hash"],
            )
            _validate_expected_revisions(request.expected_revisions, observed)
            receipt = StrategicOperationReceipt(
                project_id=request.project_id,
                turn_task_id=request.turn_task_id,
                authority_generation=int(request.authority_generation),
                operation_id=str(request.operation_id).strip(),
                operation_kind=str(request.operation_kind).strip(),
                capability=str(request.capability).strip(),
                risk_level=str(request.risk).strip().lower(),
                status=_UNSETTLED,
                request_hash=fingerprint,
                expected_revisions=dict(request.expected_revisions or {}),
                observed_revisions_before=observed,
                receipt={},
                # Pre-seeded so a crash leaves a declarable unsettled receipt
                # (reconcile requires unsettled receipts to carry a non-empty
                # unknown_effects list).
                unknown_effects=[{"effect_id": effect_id, "kind": effect_kind}],
            )
            session.add(receipt)
            await session.flush()
            receipt_id = receipt.id

    # --- Claim: atomically transition applying -> reconciling so exactly one
    # worker performs the external effect. A concurrent invocation (or a retry
    # racing an in-flight perform) that loses this compare-and-set does NOT
    # perform; it returns the current receipt. A crash after this claim leaves a
    # ``reconciling`` receipt that the reconcile/failure machinery surfaces
    # (never auto-re-performed), which is the conservative choice for a side
    # effect that may already have happened.
    async with factory() as session, session.begin():
        receipt = (
            await session.execute(
                select(StrategicOperationReceipt)
                .where(StrategicOperationReceipt.id == receipt_id)
                .with_for_update()
            )
        ).scalar_one()
        if (
            receipt.status in _TERMINAL
            or receipt.status in _REQUIRES_RECONCILIATION
        ):
            # Already settled, or another worker owns the in-flight perform.
            return receipt
        receipt.status = _CLAIMED
        receipt.updated_at = _utcnow()
        session.add(receipt)

    # --- Phase 2: perform the idempotent external effect (no lock held) ---
    async with factory() as effect_session:
        try:
            result = await perform(effect_session)
            outcome = "reconciled"
            body: dict[str, Any] = {"external_effect": result}
        except ExternalEffectNotPerformed as exc:
            outcome = "failed"
            body = {"error": _scrub_effect_error(exc)}
        except Exception as exc:  # noqa: BLE001 - unknown post-effect boundary
            # A client exception cannot prove the service did not apply the
            # effect before its response was lost. Require reconciliation.
            outcome = "unknown_effect"
            body = {"error": _scrub_effect_error(exc)}

    # --- Phase 3: settle the receipt (re-lock; a concurrent retry may win) ---
    async with factory() as session, session.begin():
        receipt = (
            await session.execute(
                select(StrategicOperationReceipt)
                .where(StrategicOperationReceipt.id == receipt_id)
                .with_for_update()
            )
        ).scalar_one()
        if receipt.status in _TERMINAL:
            return receipt
        receipt.status = outcome
        receipt.receipt = {"schema_version": 1, "structured_result": body}
        if outcome in {"reconciled", "failed"}:
            receipt.unknown_effects = []
        settled_at = _utcnow()
        receipt.completed_at = settled_at if outcome in _TERMINAL else None
        receipt.updated_at = settled_at
        session.add(receipt)
    return receipt


__all__ = [
    "ExternalEffect",
    "ExternalEffectNotPerformed",
    "execute_external_effect_operation",
]
