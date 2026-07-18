"""Resident-only reconciliation of one bounded strategic turn result."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    MessageHandlingState,
    MessageSurface,
    MessageType,
    Project,
    ProjectMessage,
    StrategicResident,
    StrategicResidentCheckpoint,
    Task,
    TaskStatus,
)
from app.project_message_creation import build_project_message
from app.strategic_operation_authority_validation import (
    RESIDENT_ENGINE_OWNER,
    RESIDENT_TURN_TASK_TYPE,
    AuthorityGenerationMismatch,
    ExpectedRevisionMismatch,
    InactiveTurnAuthority,
    OperationIdentityCollision,
    _enum_value,
    _task_is_mutation_authorized,
    _validate_frozen_turn_lineage,
    _validated_mutation_mapping,
)
from app.strategic_resident_checkpointing import CHECKPOINT_MAX_BYTES, CHECKPOINT_NAMESPACE
from app.strategic_resident_checkpoint_validation import (
    ResidentCheckpointIntegrityError,
    verify_locked_current_checkpoint,
)
from app.strategic_resident_context import reconstruct_authoritative_turn_input
from app.strategic_resident_evidence_validation import build_trusted_evidence_index
from app.strategic_resident_worker_proposal_intake import (
    drain_worker_memory_proposals,
)
from app.strategic_resident_agent_binding import (
    ResidentAgentBinding,
    ResidentAgentBindingError,
    validate_resident_agent_binding,
)
from app.strategic_resident_reconciliation_validation import (
    DecisiveEvidenceRequired,
    ReconciliationResultInvalid,
    bounded_reconciliation_ids,
    canonical_json_bytes,
    indexed_unique,
    reconciliation_request_hash,
    validate_evidence_acceptance,
)
from app.strategic_resident_receipt_validation import validate_operation_receipts
from app.strategic_resident_reconciliation_scheduling import (
    schedule_meaningful_follow_up,
)
from app.strategic_resident_turn_results import (
    ResidentEvidenceInspection,
    ResidentMemoryProposal,
    StrategicResidentTurnResult,
    normalize_strategic_resident_turn_result,
)
from app.strategic_resident_message_tail import (
    ReconciliationCursorError,
    ensure_message_tail_successor,
    represented_cursors,
)
from app.strategic_resident_message_handling import (
    respond_to_represented_user_messages,
)
from app.strategic_resident_promotion import promote_resident_successor
from app.strategic_resident_failure import reset_resident_failure_recovery
from app.strategic_resident_runtime import is_authoritative_resident_turn
from app.strategic_resident_slot_release import stage_resident_turn_slot_release
from app.strategic_resident_terminal import release_slot_after_reconciliation
from app.strategic_resident_turns import INPUT_FROZEN_CONTEXT_KEY, _load_pointer_tasks_locked
from app.time_utils import _utcnow


_RECONCILIATION_SCHEMA_VERSION = 1
@dataclass(frozen=True)
class StrategicResidentReconcileRequest:
    project_id: uuid.UUID
    turn_task_id: uuid.UUID
    authority_generation: int
    reconciliation_id: str
    expected_turn_task_version: int
    expected_checkpoint_id: uuid.UUID
    expected_checkpoint_version: int
    expected_checkpoint_hash: str
    context_hash: str
    agent_binding: ResidentAgentBinding
    turn_result: StrategicResidentTurnResult | Mapping[str, Any]
    accepted_memory_proposal_ids: tuple[str, ...] = ()
    accepted_implication_ids: tuple[str, ...] = ()
    accepted_open_loop_ids: tuple[str, ...] = ()
    resolved_open_loop_ids: tuple[str, ...] = ()
    evidence_inspections: tuple[ResidentEvidenceInspection, ...] = ()


@dataclass(frozen=True)
class StrategicResidentReconcileOutcome:
    checkpoint_id: uuid.UUID
    checkpoint_version: int
    checkpoint_hash: str
    reply_message_id: uuid.UUID | None
    promoted_task_id: uuid.UUID | None
    idempotent_replay: bool
    responded_message_ids: tuple[uuid.UUID, ...] = ()


def _deep_merge(base: Mapping[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(base))
    for raw_key, value in updates.items():
        key = str(raw_key)
        if not key or key.startswith("_"):
            raise ReconciliationResultInvalid(
                "strategic memory keys must be public non-empty strings"
            )
        existing = result.get(key)
        result[key] = (
            _deep_merge(existing, value)
            if isinstance(existing, Mapping) and isinstance(value, Mapping)
            else deepcopy(value)
        )
    return result


def _apply_memory_proposals(
    body: dict[str, Any],
    proposals: list[ResidentMemoryProposal],
) -> None:
    charter = body.get("charter")
    working_strategy = body.get("working_strategy")
    if not isinstance(charter, Mapping) or not isinstance(working_strategy, Mapping):
        raise ReconciliationResultInvalid("current checkpoint strategy is malformed")
    next_charter = dict(charter)
    next_strategy = dict(working_strategy)
    for proposal in proposals:
        updates = _validated_mutation_mapping(
            f"proposed_memory.{proposal.proposal_id}",
            proposal.updates,
        )
        if proposal.layer == "charter":
            next_charter = _deep_merge(next_charter, updates)
        else:
            next_strategy = _deep_merge(next_strategy, updates)
    body["charter"] = next_charter
    body["working_strategy"] = next_strategy


def _apply_open_loops(
    body: dict[str, Any],
    result: StrategicResidentTurnResult,
    request: StrategicResidentReconcileRequest,
) -> None:
    accepted_ids = bounded_reconciliation_ids(
        request.accepted_open_loop_ids,
        name="accepted_open_loop_ids",
    )
    resolved_ids = set(
        bounded_reconciliation_ids(
            request.resolved_open_loop_ids,
            name="resolved_open_loop_ids",
        )
    )
    strategy = body.get("working_strategy")
    if not isinstance(strategy, dict):
        raise ReconciliationResultInvalid("working_strategy is malformed")
    current: dict[str, dict[str, Any]] = {}
    for item in list(strategy.get("open_loops") or [])[:100]:
        if isinstance(item, Mapping) and str(item.get("loop_id") or ""):
            current[str(item["loop_id"])] = deepcopy(dict(item))
    unknown_resolved = resolved_ids - set(current)
    if unknown_resolved:
        raise ReconciliationResultInvalid(
            f"resolved open loops are absent: {sorted(unknown_resolved)}"
        )
    for loop_id in resolved_ids:
        current.pop(loop_id, None)

    proposed = indexed_unique(
        list(result.follow_up.open_loops) if result.follow_up else [],
        "loop_id",
        name="open loop",
    )
    for loop_id in accepted_ids:
        item = proposed.get(loop_id)
        if item is None:
            raise ReconciliationResultInvalid(
                f"accepted open loop is absent: {loop_id}"
            )
        current[loop_id] = item.model_dump(mode="json")
    strategy["open_loops"] = [current[key] for key in sorted(current)][:100]


def _next_authoritative_successor(
    resident: StrategicResident,
    tasks: dict[uuid.UUID, Task],
) -> Task | None:
    for pointer in (
        resident.interactive_successor_task_id,
        resident.background_successor_task_id,
    ):
        task = tasks.get(pointer)
        if is_authoritative_resident_turn(
            task,
            project_id=resident.project_id,
            mutable_successor=True,
        ):
            return task
    return None


def _record_unknown_effect_recovery(
    resident: StrategicResident,
    body: dict[str, Any],
    *,
    result: StrategicResidentTurnResult,
    receipt_ids: tuple[str, ...],
    turn_task_id: uuid.UUID,
    now: Any,
) -> None:
    if not receipt_ids:
        return
    attention = [item.model_dump(mode="json") for item in result.required_attention]
    recovery = body.get("recovery")
    recovery = dict(recovery) if isinstance(recovery, Mapping) else {}
    recovery.update(
        {
            "status": "unknown_effect",
            "unknown_operation_receipt_ids": list(receipt_ids),
            "unknown_effect_ids": [item.effect_id for item in result.unknown_effects],
            "required_attention": attention,
            "source_turn_task_id": str(turn_task_id),
            "updated_at": now.isoformat(),
        }
    )
    body["recovery"] = recovery
    resident.recovery_state = {
        **dict(resident.recovery_state or {}),
        **recovery,
    }
    resident.health_status = "blocked"
    resident.next_wakeup_at = None


async def _existing_reconciliation(
    session: AsyncSession,
    request: StrategicResidentReconcileRequest,
    result_hash: str,
    request_hash: str,
) -> StrategicResidentReconcileOutcome | None:
    rows = (
        (
            await session.execute(
                select(StrategicResidentCheckpoint)
                .where(
                    StrategicResidentCheckpoint.project_id == request.project_id,
                    StrategicResidentCheckpoint.source_turn_task_id
                    == request.turn_task_id,
                )
                .order_by(StrategicResidentCheckpoint.version.desc())
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    checkpoint = rows[0]
    metadata = (
        checkpoint.checkpoint.get("reconciliation")
        if isinstance(checkpoint.checkpoint, dict)
        else None
    )
    if (
        len(rows) != 1
        or not isinstance(metadata, Mapping)
        or str(metadata.get("reconciliation_id") or "")
        != str(request.reconciliation_id).strip()
        or str(metadata.get("result_hash") or "") != result_hash
        or str(metadata.get("request_hash") or "") != request_hash
    ):
        raise OperationIdentityCollision(
            "turn task already has a different reconciliation",
            turn_task_id=str(request.turn_task_id),
        )
    reply_id = metadata.get("reply_message_id")
    promoted_id = metadata.get("promoted_task_id")
    return StrategicResidentReconcileOutcome(
        checkpoint_id=checkpoint.id,
        checkpoint_version=int(checkpoint.version),
        checkpoint_hash=str(checkpoint.content_hash),
        reply_message_id=uuid.UUID(str(reply_id)) if reply_id else None,
        promoted_task_id=uuid.UUID(str(promoted_id)) if promoted_id else None,
        idempotent_replay=True,
    )


async def _create_resident_reply(
    session: AsyncSession,
    *,
    request: StrategicResidentReconcileRequest,
    result: StrategicResidentTurnResult,
    task: Task,
    reply_target_id: uuid.UUID | None,
) -> ProjectMessage | None:
    payload = task.first_payload if isinstance(task.first_payload, dict) else {}
    has_operator_input = payload.get("message_sequence_end") is not None
    if has_operator_input and not (result.reply or "").strip():
        raise ReconciliationResultInvalid(
            "interactive Resident turn result requires a user-facing reply"
        )
    has_outcome = bool(
        result.reply
        or result.operations
        or result.receipts
        or result.unknown_effects
        or result.required_attention
    )
    if not has_outcome:
        return None
    identity = hashlib.sha256(
        f"{request.turn_task_id}:{request.reconciliation_id}".encode()
    ).hexdigest()
    return await build_project_message(
        session,
        project_id=request.project_id,
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.RESPONDED,
        handled_by_task_id=request.turn_task_id,
        reply_to_message_id=reply_target_id,
        message_type=MessageType.NOTE,
        from_type="strategic",
        from_task_id=request.turn_task_id,
        content=(result.reply or "Resident operation outcome recorded.").strip(),
        context={
            "event_type": "resident_reply",
            "resident_turn_result_version": 1,
            "reconciliation_id": str(request.reconciliation_id).strip(),
            "operation_outcome": {
                "operation_ids": [item.operation_id for item in result.operations],
                "receipt_ids": [item.receipt_id for item in result.receipts],
                "unknown_effect_ids": [
                    item.effect_id for item in result.unknown_effects
                ],
                "required_attention_ids": [
                    item.attention_id for item in result.required_attention
                ],
            },
        },
        status="resolved",
        resolved_at=_utcnow(),
        updated_at=_utcnow(),
        idempotency_key=f"resident-reconciliation:{identity}",
    )


@release_slot_after_reconciliation
async def reconcile_strategic_resident_turn(
    session_factory: async_sessionmaker[AsyncSession] | AsyncSession,
    request: StrategicResidentReconcileRequest,
) -> StrategicResidentReconcileOutcome:
    """Atomically accept one bounded result under current Resident authority."""

    reconciliation_id = str(request.reconciliation_id).strip()
    if not reconciliation_id or len(reconciliation_id) > 200:
        raise ReconciliationResultInvalid("reconciliation_id is invalid")
    result = normalize_strategic_resident_turn_result(request.turn_result)
    result_projection = result.model_dump(mode="json")
    result_hash = hashlib.sha256(canonical_json_bytes(result_projection)).hexdigest()
    request_hash = reconciliation_request_hash(request, result_hash=result_hash)
    private_factory = (
        async_sessionmaker(
            bind=session_factory.bind,
            expire_on_commit=False,
            autoflush=False,
        )
        if isinstance(session_factory, AsyncSession)
        else session_factory
    )

    async with private_factory() as session, session.begin():
        project = (
            await session.execute(
                select(Project)
                .where(Project.id == request.project_id)
                .with_for_update(key_share=True)
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
        locked_task = (
            await session.execute(
                select(Task)
                .where(Task.id == request.turn_task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if locked_task is None:
            raise ResidentAgentBindingError("Resident turn task not found")
        validate_resident_agent_binding(
            locked_task,
            request.agent_binding,
            allowed_statuses=frozenset(
                {TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED}
            ),
        )
        existing = await _existing_reconciliation(
            session,
            request,
            result_hash,
            request_hash,
        )
        if existing is not None:
            return existing
        tasks = await _load_pointer_tasks_locked(session, resident)
        task = tasks.get(request.turn_task_id)
        if _enum_value(resident.engine_owner) != RESIDENT_ENGINE_OWNER:
            raise InactiveTurnAuthority("Strategic Resident does not own the engine")
        if int(resident.authority_generation) != int(request.authority_generation):
            raise AuthorityGenerationMismatch("Resident generation is stale")
        if resident.active_turn_task_id != request.turn_task_id or task is None:
            raise InactiveTurnAuthority("turn task is not the active Resident turn")
        if task.project_id != request.project_id:
            raise InactiveTurnAuthority("turn task belongs to another project")
        if str((task.first_payload or {}).get("type") or "") != RESIDENT_TURN_TASK_TYPE:
            raise InactiveTurnAuthority("task is not a strategic_resident_turn")
        if _enum_value(task.status) != _enum_value(TaskStatus.IN_PROGRESS):
            raise InactiveTurnAuthority("turn task is not in progress")
        if not _task_is_mutation_authorized(task):
            raise InactiveTurnAuthority("turn task is not mutation-authorized")
        if int(task.version) != int(request.expected_turn_task_version):
            raise ExpectedRevisionMismatch("turn task version changed")
        receipt_validation = await validate_operation_receipts(
            session,
            result,
            request,
            project_id=request.project_id,
            turn_task_id=request.turn_task_id,
            authority_generation=int(resident.authority_generation),
        )
        checkpoint = (
            await session.execute(
                select(StrategicResidentCheckpoint)
                .where(
                    StrategicResidentCheckpoint.id == resident.current_checkpoint_id
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if checkpoint is None or checkpoint.project_id != request.project_id:
            raise InactiveTurnAuthority("current Resident checkpoint is missing")
        if (
            checkpoint.id != request.expected_checkpoint_id
            or int(checkpoint.version) != int(request.expected_checkpoint_version)
            or str(checkpoint.content_hash) != str(request.expected_checkpoint_hash)
        ):
            raise ExpectedRevisionMismatch("current checkpoint CAS failed")
        _validate_frozen_turn_lineage(
            task,
            project_id=request.project_id,
            engine_owner=_enum_value(resident.engine_owner),
            authority_generation=int(resident.authority_generation),
            checkpoint_version=int(checkpoint.version),
            checkpoint_hash=str(checkpoint.content_hash),
        )
        provider_input = await reconstruct_authoritative_turn_input(
            session,
            project_id=request.project_id,
            turn_task_id=request.turn_task_id,
        )
        if provider_input["lineage"].get("context_hash") != request.context_hash:
            raise ExpectedRevisionMismatch("Resident turn context lineage changed")
        trusted_evidence = build_trusted_evidence_index(
            provider_input,
            receipt_validation.receipts,
            expected_context_hash=request.context_hash,
        )
        accepted_proposals, accepted_implication_ids = validate_evidence_acceptance(
            result,
            request,
            trusted_evidence=trusted_evidence,
        )
        unsettled_receipt_ids = receipt_validation.unsettled_receipt_ids

        window = provider_input["lane_context"]["conversation_window"]
        represented_end = window.get("represented_end")
        message_cursor, project_cursor = represented_cursors(
            task,
            resident,
            represented_message_end=represented_end,
        )
        payload = task.first_payload if isinstance(task.first_payload, dict) else {}
        requested_end = payload.get("message_sequence_end")
        if isinstance(requested_end, int):
            await ensure_message_tail_successor(
                session,
                resident,
                tasks,
                represented_end=message_cursor,
                requested_end=requested_end,
            )
        try:
            verified_checkpoint = await verify_locked_current_checkpoint(
                session,
                resident,
                checkpoint,
            )
        except ResidentCheckpointIntegrityError as exc:
            raise ExpectedRevisionMismatch(
                "current checkpoint integrity validation failed"
            ) from exc
        body = verified_checkpoint.mutable_body()
        # Admit any pending worker (StrategicCycle) memory proposals through the
        # SAME checkpoint-CAS apply path — the only channel by which a bounded
        # worker's proposal changes strategic memory. Revision-linked to this
        # exact checkpoint and evidence-gated; stale/untrusted ones are rejected
        # in-place without failing the turn.
        admitted_worker_proposals = await drain_worker_memory_proposals(
            session,
            project_id=request.project_id,
            current_checkpoint_version=int(checkpoint.version),
            current_checkpoint_hash=str(checkpoint.content_hash),
            trusted_evidence=trusted_evidence,
        )
        _apply_memory_proposals(
            body, [*accepted_proposals, *admitted_worker_proposals]
        )
        _apply_open_loops(body, result, request)
        now = _utcnow()
        _record_unknown_effect_recovery(
            resident,
            body,
            result=result,
            receipt_ids=unsettled_receipt_ids,
            turn_task_id=request.turn_task_id,
            now=now,
        )
        cursors = body.setdefault("cursors", {})
        if not isinstance(cursors, dict):
            raise ReconciliationCursorError("checkpoint cursors are malformed")
        cursors["handled_message_sequence"] = message_cursor
        cursors["project_change"] = project_cursor

        responded_message_ids: tuple[uuid.UUID, ...] = ()
        if requested_end is not None:
            message_start = window.get("requested_start")
            try:
                represented_start = int(message_start)
            except (TypeError, ValueError):
                raise ReconciliationCursorError(
                    "turn reply-association cursor is malformed"
                ) from None
            responded_message_ids = await respond_to_represented_user_messages(
                session,
                project_id=request.project_id,
                turn_task_id=request.turn_task_id,
                represented_start=represented_start,
                represented_end=message_cursor,
            )
        reply_target_id = (
            responded_message_ids[-1] if responded_message_ids else None
        )
        reply = await _create_resident_reply(
            session,
            request=request,
            result=result,
            task=task,
            reply_target_id=reply_target_id,
        )
        if reply is not None:
            session.add(reply)
            await session.flush()

        task.plan_output = {
            "strategic_resident_turn_result": result_projection,
            "result_hash": result_hash,
            "reconciliation_id": reconciliation_id,
            "unsettled_operation_receipt_ids": list(unsettled_receipt_ids),
        }
        # NOTE: the turn task is completed AFTER the checkpoint rotation below is
        # flushed (see the second flush) — never in the same flush. The
        # checkpoint-insert and checkpoint-rotation fences authorize a rotation
        # only while its turn task is still IN_PROGRESS; completing it in the
        # same flush lets the task->COMPLETED UPDATE land before the resident
        # rotation UPDATE, so the fence sees a COMPLETED turn and rejects the
        # (legitimate) rotation with 42501 on PostgreSQL.
        task_context = dict(task.context or {})
        task_context[INPUT_FROZEN_CONTEXT_KEY] = True
        task.context = task_context
        stage_resident_turn_slot_release(task)
        session.add(task)

        promotion_candidate = (
            None
            if unsettled_receipt_ids
            else _next_authoritative_successor(resident, tasks)
        )
        new_version = int(checkpoint.version) + 1
        metadata = {
            "schema_version": _RECONCILIATION_SCHEMA_VERSION,
            "reconciliation_id": reconciliation_id,
            "result_hash": result_hash,
            "request_hash": request_hash,
            "source_turn_task_id": str(request.turn_task_id),
            "authority_generation": int(request.authority_generation),
            "accepted_memory_proposal_ids": [
                proposal.proposal_id for proposal in accepted_proposals
            ],
            "accepted_implication_ids": accepted_implication_ids,
            "inspected_evidence_ids": sorted(
                inspection.evidence_id for inspection in request.evidence_inspections
            ),
            "unsettled_operation_receipt_ids": list(unsettled_receipt_ids),
            "reply_message_id": str(reply.id) if reply is not None else None,
            "promoted_task_id": (
                str(promotion_candidate.id)
                if promotion_candidate is not None
                else None
            ),
        }
        body["schema_version"] = max(int(body.get("schema_version") or 1), 1)
        body["reconciliation"] = metadata
        encoded = canonical_json_bytes(body)
        if len(encoded) > CHECKPOINT_MAX_BYTES:
            raise ReconciliationResultInvalid(
                f"reconciled checkpoint exceeds {CHECKPOINT_MAX_BYTES} bytes"
            )
        content_hash = hashlib.sha256(encoded).hexdigest()
        checkpoint_id = uuid.uuid5(
            CHECKPOINT_NAMESPACE,
            f"{request.project_id}:{new_version}:{reconciliation_id}:{result_hash}",
        )
        next_checkpoint = StrategicResidentCheckpoint(
            id=checkpoint_id,
            project_id=request.project_id,
            version=new_version,
            schema_version=int(body["schema_version"]),
            content_hash=content_hash,
            checkpoint=body,
            authority_generation=int(resident.authority_generation),
            source_turn_task_id=request.turn_task_id,
        )
        session.add(next_checkpoint)
        # Insert the new checkpoint BEFORE the resident rotation below. The
        # resident<->checkpoint FK cycle is expressed as raw columns (no ORM
        # relationship), so the unit of work does not order the checkpoint INSERT
        # ahead of the resident UPDATE on its own. If the rotation UPDATE flushed
        # first, the rotation fence would look up NEW.current_checkpoint_id (this
        # not-yet-inserted row), find nothing, and reject the rotation as
        # unauthorized (42501) on PostgreSQL. The turn task is still IN_PROGRESS
        # here, so the checkpoint-insert fence authorizes this row.
        await session.flush()
        resident.previous_checkpoint_id = checkpoint.id
        resident.current_checkpoint_id = checkpoint_id
        resident.handled_message_sequence = message_cursor
        resident.handled_project_change_cursor = project_cursor
        if not unsettled_receipt_ids:
            reset_resident_failure_recovery(resident, recovered_at=now)
        schedule_meaningful_follow_up(
            resident,
            body=body,
            result=result,
            turn_task_id=request.turn_task_id,
            now=now,
        )
        if unsettled_receipt_ids:
            resident.next_wakeup_at = None
        resident.active_turn_task_id = None
        resident.active_turn_lane = None
        promoted = None
        if promotion_candidate is not None:
            promoted = promote_resident_successor(
                resident,
                tasks,
                mutation_authorized=True,
                checkpoint_version=new_version,
                checkpoint_hash=content_hash,
                authority_generation=int(resident.authority_generation),
                engine_owner=_enum_value(resident.engine_owner),
            )
            if promoted is None or promoted.id != promotion_candidate.id:
                raise ReconciliationResultInvalid(
                    "Resident successor changed during reconciliation"
                )
        resident.updated_at = now
        session.add(resident)
        # Flush the checkpoint insert + resident rotation while the turn task is
        # still IN_PROGRESS so the rotation/checkpoint fences authorize it.
        await session.flush()
        # Only now complete the turn: its result is durably checkpointed, and
        # the rotation it authorized has already been fenced-in.
        task.status = TaskStatus.COMPLETED
        task.completed_at = _utcnow()
        task.latest_status_message = "Resident turn reconciled into checkpoint"
        session.add(task)
        await session.flush()
        return StrategicResidentReconcileOutcome(
            checkpoint_id=checkpoint_id,
            checkpoint_version=new_version,
            checkpoint_hash=content_hash,
            reply_message_id=reply.id if reply is not None else None,
            promoted_task_id=promoted.id if promoted is not None else None,
            idempotent_replay=False,
            responded_message_ids=responded_message_ids,
        )
__all__ = [
    "DecisiveEvidenceRequired",
    "ReconciliationCursorError",
    "ReconciliationResultInvalid",
    "StrategicResidentReconcileOutcome",
    "StrategicResidentReconcileRequest",
    "reconcile_strategic_resident_turn",
]
