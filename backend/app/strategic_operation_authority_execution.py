"""Transactional execution for database-only Strategic Resident operations."""

from __future__ import annotations

import inspect
from copy import deepcopy
from types import MappingProxyType
from collections.abc import Mapping

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    Project,
    StrategicOperationReceipt,
    Task,
)
from app.strategic_operation_authority_validation import (
    RESIDENT_TURN_TASK_TYPE,
    AuthorityBoundaryMutation,
    FencedMutationWriter,
    FencedOperationContext,
    FencedOperationRequest,
    FencedOperationUsageError,
    FencedTaskWriter,
    InactiveTurnAuthority,
    MutationCallback,
    OperationIdentityCollision,
    assert_locked_turn_mutation_authority,
    _REQUIRED_AUTHORITY_REVISIONS,
    _TERMINAL_RECEIPT_STATUSES,
    _enum_value,
    _freeze_json,
    _observed_revisions,
    _request_fingerprint,
    _require_expected_revision_keys,
    _validate_expected_revisions,
    _validate_frozen_turn_lineage,
    _validate_frozen_turn_lineage_values,
    _validated_request_payload,
)
from app.strategic_operation_result_bounds import bounded_result
from app.time_utils import _utcnow
from app.strategic_resident_managed_task import (
    ManagedTaskCreateWriter,
    materialize_managed_task,
)


async def _terminal_receipt_fast_path(
    session: AsyncSession,
    request: FencedOperationRequest,
    fingerprint: str,
) -> StrategicOperationReceipt | None:
    """Adopt an immutable terminal receipt without taking authority locks."""

    existing = (
        await session.execute(
            select(StrategicOperationReceipt).where(
                StrategicOperationReceipt.project_id == request.project_id,
                StrategicOperationReceipt.operation_id
                == str(request.operation_id).strip(),
            )
        )
    ).scalar_one_or_none()
    if existing is None or existing.status not in _TERMINAL_RECEIPT_STATUSES:
        return None
    if existing.request_hash != fingerprint:
        raise OperationIdentityCollision(
            "operation_id already belongs to a different request",
            operation_id=request.operation_id,
        )
    return existing


async def execute_fenced_db_operation(
    session_factory: async_sessionmaker[AsyncSession] | AsyncSession,
    request: FencedOperationRequest,
    mutate: MutationCallback,
) -> StrategicOperationReceipt:
    """Execute one database-only mutation under Resident authority fencing.

    Lock order is Project -> StrategicResident -> active Task -> existing
    StrategicOperationReceipt. The function owns the transaction and commits
    the mutation and applied receipt atomically.
    """

    request_payload = _validated_request_payload(request.request_payload)
    fingerprint = _request_fingerprint(
        request,
        request_payload=request_payload,
    )
    private_session_factory = (
        async_sessionmaker(
            bind=session_factory.bind,
            expire_on_commit=False,
            autoflush=False,
        )
        if isinstance(session_factory, AsyncSession)
        else session_factory
    )

    # Deferred import lets the authority service remain independently testable
    # while the StrategicResident schema is assembled by the control-plane slice.
    from app.models import StrategicResident

    async with private_session_factory() as session, session.begin():
        terminal_receipt = await _terminal_receipt_fast_path(
            session,
            request,
            fingerprint,
        )
        if terminal_receipt is not None:
            return terminal_receipt
        _require_expected_revision_keys(
            request.expected_revisions,
            _REQUIRED_AUTHORITY_REVISIONS,
            reason="mutation-authorized operation",
        )

        project = (
            await session.execute(
                select(Project)
                .where(Project.id == request.project_id)
                .with_for_update(key_share=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if project is None:
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
            return existing

        observed_generation = assert_locked_turn_mutation_authority(
            resident, turn_task, request
        )

        observed = await _observed_revisions(
            session,
            project,
            resident,
            turn_task,
            request.expected_revisions,
        )
        # Optional bounded mutation of one non-turn target task, CAS'd on its
        # ``updated_at`` (advances on every write; ``version`` is forensic-only).
        # Loaded and locked here so its state is stable through the apply.
        target_task: Task | None = None
        if request.target_task_id is not None:
            if request.target_task_id == request.turn_task_id:
                raise AuthorityBoundaryMutation(
                    "operation cannot target its own Resident turn task"
                )
            _require_expected_revision_keys(
                request.expected_revisions,
                {"target_task_updated_at"},
                reason="target-task operation",
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
                target_task.updated_at.isoformat() if target_task.updated_at else None
            )
        _validate_frozen_turn_lineage(
            turn_task,
            project_id=request.project_id,
            engine_owner=_enum_value(resident.engine_owner),
            authority_generation=observed_generation,
            checkpoint_version=observed["resident_checkpoint_version"],
            checkpoint_hash=observed["resident_checkpoint_hash"],
        )
        _validate_expected_revisions(request.expected_revisions, observed)

        receipt = StrategicOperationReceipt(
            project_id=request.project_id,
            turn_task_id=request.turn_task_id,
            operation_id=str(request.operation_id).strip(),
            authority_generation=int(request.authority_generation),
            operation_kind=str(request.operation_kind).strip(),
            capability=str(request.capability).strip(),
            risk_level=str(request.risk).strip().lower(),
            status="applying",
            request_hash=fingerprint,
            expected_revisions=dict(request.expected_revisions or {}),
            observed_revisions_before=observed,
            receipt={},
            unknown_effects=[],
        )
        session.add(receipt)
        await session.flush()

        writer = FencedMutationWriter({})
        task_writer = FencedTaskWriter({}) if target_task is not None else None
        managed_task_writer = ManagedTaskCreateWriter(
            receipt.id,
            authorized_capability=receipt.capability,
        )
        context = FencedOperationContext(
            project_id=project.id,
            turn_task_id=turn_task.id,
            receipt_id=receipt.id,
            operation_id=receipt.operation_id,
            authority_generation=int(resident.authority_generation),
            request_payload=_freeze_json(request_payload),
            observed_revisions=MappingProxyType(deepcopy(observed)),
            writer=writer,
            managed_task_writer=managed_task_writer,
            task_writer=task_writer,
        )
        authority_snapshot = (
            resident.project_id,
            _enum_value(resident.engine_owner),
            int(resident.authority_generation),
            resident.active_turn_task_id,
            resident.current_checkpoint_id,
            turn_task.id,
            turn_task.project_id,
            int(turn_task.version),
            _enum_value(turn_task.status),
            deepcopy(turn_task.first_payload or {}),
            deepcopy(turn_task.context or {}),
            receipt.project_id,
            receipt.turn_task_id,
            receipt.authority_generation,
            receipt.operation_id,
            receipt.operation_kind,
            receipt.capability,
            receipt.risk_level,
            receipt.request_hash,
            dict(receipt.expected_revisions or {}),
            list(receipt.unknown_effects or []),
        )
        try:
            raw_result = mutate(context)
            if inspect.isawaitable(raw_result):
                if inspect.iscoroutine(raw_result):
                    raw_result.close()
                raise FencedOperationUsageError(
                    "mutation callback must be synchronous and database-only"
                )
        except BaseException:
            session.sync_session.expunge_all()
            raise
        try:
            await session.flush()
            resident_after = (
                await session.execute(
                    select(
                        StrategicResident.project_id,
                        StrategicResident.engine_owner,
                        StrategicResident.authority_generation,
                        StrategicResident.active_turn_task_id,
                        StrategicResident.current_checkpoint_id,
                    ).where(StrategicResident.project_id == request.project_id)
                )
            ).one()
            task_after = (
                await session.execute(
                    select(
                        Task.id,
                        Task.project_id,
                        Task.version,
                        Task.status,
                        Task.payloads,
                        Task.context,
                    ).where(Task.id == request.turn_task_id)
                )
            ).one()
            receipt_after = (
                await session.execute(
                    select(
                        StrategicOperationReceipt.project_id,
                        StrategicOperationReceipt.turn_task_id,
                        StrategicOperationReceipt.authority_generation,
                        StrategicOperationReceipt.operation_id,
                        StrategicOperationReceipt.operation_kind,
                        StrategicOperationReceipt.capability,
                        StrategicOperationReceipt.risk_level,
                        StrategicOperationReceipt.request_hash,
                        StrategicOperationReceipt.expected_revisions,
                        StrategicOperationReceipt.unknown_effects,
                    ).where(StrategicOperationReceipt.id == receipt.id)
                )
            ).one()
            task_payloads_after = (
                task_after.payloads if isinstance(task_after.payloads, list) else []
            )
            task_context_after = (
                task_after.context if isinstance(task_after.context, dict) else {}
            )
            authority_after = (
                resident_after.project_id,
                _enum_value(resident_after.engine_owner),
                int(resident_after.authority_generation),
                resident_after.active_turn_task_id,
                resident_after.current_checkpoint_id,
                task_after.id,
                task_after.project_id,
                int(task_after.version),
                _enum_value(task_after.status),
                deepcopy(task_payloads_after[0] if task_payloads_after else {}),
                deepcopy(task_context_after),
                receipt_after.project_id,
                receipt_after.turn_task_id,
                receipt_after.authority_generation,
                receipt_after.operation_id,
                receipt_after.operation_kind,
                receipt_after.capability,
                receipt_after.risk_level,
                receipt_after.request_hash,
                dict(receipt_after.expected_revisions or {}),
                list(receipt_after.unknown_effects or []),
            )
            if (
                not task_payloads_after
                or not isinstance(task_payloads_after[0], dict)
                or str(task_payloads_after[0].get("type") or "")
                != RESIDENT_TURN_TASK_TYPE
            ):
                raise AuthorityBoundaryMutation(
                    "mutation callback changed Resident turn type"
                )
            try:
                _validate_frozen_turn_lineage_values(
                    task_payloads_after[0],
                    task_context_after,
                    project_id=request.project_id,
                    engine_owner=_enum_value(resident_after.engine_owner),
                    authority_generation=int(resident_after.authority_generation),
                    checkpoint_version=observed["resident_checkpoint_version"],
                    checkpoint_hash=observed["resident_checkpoint_hash"],
                )
            except InactiveTurnAuthority as exc:
                raise AuthorityBoundaryMutation(
                    "mutation callback changed frozen Resident turn lineage"
                ) from exc
            if authority_after != authority_snapshot:
                raise AuthorityBoundaryMutation(
                    "mutation callback changed Resident authority identity"
                )
            if not isinstance(raw_result, (BaseModel, Mapping)):
                raise FencedOperationUsageError(
                    "mutation callback must return BoundedOperationResult or a mapping"
                )
            structured_result = bounded_result(raw_result)
            writer._apply(
                project,
                expected_revisions=request.expected_revisions,
            )
            session.add(project)
            managed_task_plan = managed_task_writer.validated_plan()
            if target_task is not None and task_writer is not None:
                if task_writer.has_changes():
                    task_writer._apply(target_task)
                    session.add(target_task)
                elif managed_task_plan is None:
                    # Preserve the existing fail-closed contract for ordinary
                    # target-task mutations. Managed debug creation is the one
                    # CAS-only target lock: it reads a stable source Task but
                    # deliberately does not mutate it.
                    task_writer._apply(target_task)
            if managed_task_plan is not None:
                await materialize_managed_task(
                    session,
                    plan=managed_task_plan,
                    turn_task=turn_task,
                    receipt=receipt,
                )
            await session.flush()
        except BaseException:
            session.sync_session.expunge_all()
            raise
        receipt.receipt = {
            "schema_version": 1,
            "structured_result": structured_result,
        }
        revisions_after = await _observed_revisions(
            session,
            project,
            resident,
            turn_task,
            request.expected_revisions,
        )
        if target_task is not None:
            revisions_after["target_task_updated_at"] = (
                target_task.updated_at.isoformat() if target_task.updated_at else None
            )
        receipt.observed_revisions_after = revisions_after
        receipt.status = "applied"
        receipt.completed_at = _utcnow()
        receipt.updated_at = receipt.completed_at
        session.add(receipt)

    return receipt
