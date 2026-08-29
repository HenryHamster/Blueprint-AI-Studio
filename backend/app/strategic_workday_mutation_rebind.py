"""Exact same-consultation envelope rebinds for authority-changing Workday ops.

Raw Workday prose is never mutation authority.  This module accepts only the
server-created identity of a currently leased private consultation packet,
then records and carries the exact problem-model revision committed by that
same task.  The normal authority/privacy generation fence remains in force.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, replace
from typing import Any, Mapping

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    MessageHandlingState,
    Project,
    ProjectMessage,
    StrategicContextEnvelopeSnapshot,
    Task,
)
from app.strategic_memory_sources import canonical_json_bytes
from app.time_utils import _utcnow


WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY = (
    "_workday_consultation_mutation_receipts"
)
WORKDAY_MUTATION_RECEIPTS_HAS_MORE_CONTEXT_KEY = (
    "_workday_consultation_mutation_receipts_has_more"
)
WORKDAY_MUTATION_RECEIPT_LIMIT = 64
WORKDAY_OPERATION_LEDGER_CONTEXT_KEY = "_workday_consultation_operation_ledger"
WORKDAY_OPERATION_LEDGER_LIMIT = 64
_PROBLEM_MODEL_OPS = frozenset(
    {
        "set_problem_model",
        "patch_problem_model",
        "replace_problem_model",
        "pending_confirm",
        "resolve_pending_confirm",
    }
)
_ENTITY_OPS = frozenset(
    {
        "create_direction",
        "update_direction",
        "stage_direction",
        "approve_direction",
        "activate_direction",
        "retire_direction",
        "reject_direction",
        "decide_churn_retirement",
        "spawn_task",
        "set_memory",
        "watch_task",
        "ack_watch",
        "cancel_watch",
        "cancel_task",
        "dispose_completion_debt",
        # Pipeline ACTION verbs that mutate a named entity, so a Workday
        # consultation binds a receipt to them the same way it does for
        # cancel_task. force_promote is deliberately absent: like
        # request_qualification it is a project-level TRIGGER, not an edit to a
        # direction/task entity, so it has no entity id to bind a receipt to.
        "requeue_task",
        "reset_iteration_cap",
        "set_task_priority",
        "unhold_direction",
        "create_acceptance_criterion",
    }
)
_ENTITY_KINDS = frozenset(
    {
        "direction",
        "task",
        "strategic_spawn_request",
        "knowledge_document",
        "task_watch",
        "completion_debt",
        "acceptance_criterion",
    }
)
_STATE_OP_TARGET_KINDS = {
    "set_vision": "vision",
    "set_running_update": "running_update",
    "set_version_goal": "version_goal",
    "set_version_phase": "version_phase",
}
WORKDAY_RECEIPT_BOUND_OPS = frozenset(
    _PROBLEM_MODEL_OPS | _ENTITY_OPS | set(_STATE_OP_TARGET_KINDS)
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class WorkdayConsultationOperationBinding:
    packet: ProjectMessage
    task: Task
    packet_id: uuid.UUID
    workday_claim_id: uuid.UUID
    operation_id: uuid.UUID
    fast_claim_id: uuid.UUID
    task_id: uuid.UUID
    resident_session_id: str
    worker_generation: int
    worker_incarnation: uuid.UUID
    input_revision: int
    transcript_sequence: int
    envelope_snapshot_id: uuid.UUID
    context_authority_generation_before: int
    privacy_generation: int


WorkdayProblemModelMutationBinding = WorkdayConsultationOperationBinding


@dataclass(frozen=True)
class BoundWorkdayOperation:
    session: AsyncSession
    project: Project
    task: Task
    op: str
    fingerprint: str
    binding: WorkdayConsultationOperationBinding | None

    async def record_entity(
        self, *, entity_kind: str, entity_id: str, effect: str
    ) -> None:
        await record_workday_consultation_operation(
            self.session,
            binding=self.binding,
            project=self.project,
            task=self.task,
            op=self.op,
            entity_kind=entity_kind,
            entity_id=entity_id,
            effect=effect,
            operation_fingerprint=self.fingerprint,
        )

    async def record_state(
        self, *, target_kind: str, target_revision: str | int, value: object
    ) -> None:
        await record_workday_consultation_operation(
            self.session,
            binding=self.binding,
            project=self.project,
            task=self.task,
            op=self.op,
            target_kind=target_kind,
            target_revision=target_revision,
            target_sha256=workday_state_sha256(value),
            operation_fingerprint=self.fingerprint,
        )

    async def record_problem_model(self, problem_model: Mapping[str, Any]) -> None:
        await rebind_problem_model_workday_packet(
            self.session,
            binding=self.binding,
            project=self.project,
            task=self.task,
            op=self.op,
            problem_model=problem_model,
            operation_fingerprint=self.fingerprint,
        )

    def _child_binding(self, *, op: str, key: str, value: object) -> tuple[
        WorkdayConsultationOperationBinding | None, str
    ]:
        fingerprint = workday_operation_fingerprint(op, value)
        if self.binding is None:
            return None, fingerprint
        return (
            replace(
                self.binding,
                operation_id=uuid.uuid5(self.binding.operation_id, f"{op}:{key}"),
            ),
            fingerprint,
        )

    async def record_child_entity(
        self,
        *,
        op: str,
        key: str,
        entity_kind: str,
        entity_id: str,
        effect: str,
    ) -> None:
        binding, fingerprint = self._child_binding(
            op=op,
            key=key,
            value={
                "entity_kind": entity_kind,
                "entity_id": entity_id,
                "effect": effect,
            },
        )
        await record_workday_consultation_operation(
            self.session,
            binding=binding,
            project=self.project,
            task=self.task,
            op=op,
            entity_kind=entity_kind,
            entity_id=entity_id,
            effect=effect,
            operation_fingerprint=fingerprint,
        )

    async def record_child_state(
        self,
        *,
        op: str,
        key: str,
        target_revision: str | int,
        value: object,
    ) -> None:
        binding, fingerprint = self._child_binding(op=op, key=key, value=value)
        await record_workday_consultation_operation(
            self.session,
            binding=binding,
            project=self.project,
            task=self.task,
            op=op,
            target_kind=_STATE_OP_TARGET_KINDS[op],
            target_revision=target_revision,
            target_sha256=workday_state_sha256(value),
            operation_fingerprint=fingerprint,
        )


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


def _uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _bounded_receipt(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or value.get("version") != 1:
        return None
    uuid_fields = (
        "packet_id",
        "workday_claim_id",
        "operation_id",
        "fast_claim_id",
        "task_id",
        "worker_incarnation",
        "envelope_snapshot_id",
    )
    uuids = {key: _uuid(value.get(key)) for key in uuid_fields}
    if any(item is None for item in uuids.values()):
        return None
    integer_fields = (
        "worker_generation",
        "context_authority_generation_before",
        "context_authority_generation_after",
        "privacy_generation",
    )
    integers = {key: _nonnegative_int(value.get(key)) for key in integer_fields}
    if any(item is None for item in integers.values()):
        return None
    if integers["context_authority_generation_after"] not in {
        integers["context_authority_generation_before"],
        integers["context_authority_generation_before"] + 1,
    }:
        return None
    resident_session_id = value.get("resident_session_id")
    op = value.get("op")
    target_revision = value.get("target_revision")
    target_sha256 = value.get("target_sha256")
    operation_fingerprint = value.get("operation_fingerprint")
    recorded_at = value.get("recorded_at")
    common_invalid = (
        not isinstance(resident_session_id, str)
        or not resident_session_id
        or len(resident_session_id) > 255
        or not isinstance(recorded_at, str)
        or not recorded_at
        or len(recorded_at) > 64
        or not isinstance(operation_fingerprint, str)
        or _SHA256_PATTERN.fullmatch(operation_fingerprint) is None
    )
    if common_invalid:
        return None
    receipt = {
        "version": 1,
        **{key: str(uuids[key]) for key in uuid_fields},
        "resident_session_id": resident_session_id,
        "session_id": resident_session_id,
        "incarnation_id": str(uuids["worker_incarnation"]),
        **integers,
        "op": op,
        "operation_fingerprint": operation_fingerprint,
        "recorded_at": recorded_at,
    }
    if op in _PROBLEM_MODEL_OPS and value.get("target_kind") == "problem_model":
        if (
            not isinstance(target_revision, (str, int))
            or isinstance(target_revision, bool)
            or not isinstance(target_sha256, str)
            or _SHA256_PATTERN.fullmatch(target_sha256) is None
        ):
            return None
        receipt.update(
            target_kind="problem_model",
            target_revision=target_revision,
            target_sha256=target_sha256,
        )
        return receipt
    expected_target_kind = _STATE_OP_TARGET_KINDS.get(str(op))
    if expected_target_kind is not None and value.get("target_kind") == expected_target_kind:
        if (
            not isinstance(target_revision, (str, int))
            or isinstance(target_revision, bool)
            or not isinstance(target_sha256, str)
            or _SHA256_PATTERN.fullmatch(target_sha256) is None
        ):
            return None
        receipt.update(
            target_kind=expected_target_kind,
            target_revision=target_revision,
            target_sha256=target_sha256,
        )
        return receipt
    entity_kind = value.get("entity_kind")
    entity_id = value.get("entity_id")
    effect = value.get("effect")
    accepted_at = value.get("accepted_at")
    if (
        op not in _ENTITY_OPS
        or entity_kind not in _ENTITY_KINDS
        or not isinstance(entity_id, str)
        or not entity_id
        or len(entity_id) > 255
        or effect not in {"created", "updated", "replayed", "deferred"}
        or not isinstance(accepted_at, str)
        or not accepted_at
        or len(accepted_at) > 64
    ):
        return None
    receipt.update(
        entity_kind=entity_kind,
        entity_id=entity_id,
        effect=effect,
        accepted_at=accepted_at,
    )
    return receipt


def bounded_workday_mutation_receipts(
    context: object,
    *,
    packet_id: uuid.UUID | None = None,
    workday_claim_id: uuid.UUID | None = None,
    operation_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    resident_session_id: str | None = None,
    worker_generation: int | None = None,
    worker_incarnation: uuid.UUID | None = None,
    limit: int = WORKDAY_MUTATION_RECEIPT_LIMIT,
) -> list[dict[str, Any]]:
    """Return only bounded, scalar receipts matching any supplied identity."""

    raw = (
        context.get(WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY)
        if isinstance(context, Mapping)
        else None
    )
    if not isinstance(raw, list):
        return []
    expected = {
        "packet_id": str(packet_id) if packet_id is not None else None,
        "workday_claim_id": (
            str(workday_claim_id) if workday_claim_id is not None else None
        ),
        "operation_id": str(operation_id) if operation_id is not None else None,
        "task_id": str(task_id) if task_id is not None else None,
        "resident_session_id": resident_session_id,
        "worker_generation": worker_generation,
        "worker_incarnation": (
            str(worker_incarnation) if worker_incarnation is not None else None
        ),
    }
    bounded: list[dict[str, Any]] = []
    for value in raw:
        receipt = _bounded_receipt(value)
        if receipt is None or any(
            wanted is not None and receipt.get(key) != wanted
            for key, wanted in expected.items()
        ):
            continue
        bounded.append(receipt)
        if len(bounded) >= max(1, min(WORKDAY_OPERATION_LEDGER_LIMIT, limit)):
            break
    return bounded


def workday_mutation_receipts_have_more(context: object) -> bool:
    return bool(
        isinstance(context, Mapping)
        and context.get(WORKDAY_MUTATION_RECEIPTS_HAS_MORE_CONTEXT_KEY) is True
    )


def workday_operation_fingerprint(op: str, data: object) -> str:
    return hashlib.sha256(
        canonical_json_bytes({"op": op, "data": data})
    ).hexdigest()


def workday_state_sha256(value: object) -> str:
    content = (
        value.encode("utf-8")
        if isinstance(value, str)
        else canonical_json_bytes(value)
    )
    return hashlib.sha256(content).hexdigest()


def find_workday_operation_replay(
    binding: WorkdayConsultationOperationBinding | None,
    *,
    op: str,
    operation_fingerprint: str,
) -> dict[str, Any] | None:
    if binding is None:
        return None
    packet_context = (
        binding.packet.context if isinstance(binding.packet.context, dict) else {}
    )
    raw_ledger = packet_context.get(WORKDAY_OPERATION_LEDGER_CONTEXT_KEY)
    if raw_ledger is None:
        raw_ledger = []
    if not isinstance(raw_ledger, list) or any(
        _bounded_receipt(value) is None for value in raw_ledger
    ):
        raise _conflict("Workday operation history is invalid")
    ledger_context = {
        WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY: raw_ledger
    }
    all_receipts = bounded_workday_mutation_receipts(
        ledger_context,
        packet_id=binding.packet_id,
        task_id=binding.task_id,
        resident_session_id=binding.resident_session_id,
        worker_generation=binding.worker_generation,
        limit=WORKDAY_OPERATION_LEDGER_LIMIT,
    )
    exact_id = [
        receipt for receipt in all_receipts
        if receipt["operation_id"] == str(binding.operation_id)
    ]
    if exact_id:
        receipt = exact_id[-1]
        if (
            receipt["op"] != op
            or receipt["operation_fingerprint"] != operation_fingerprint
        ):
            raise _conflict(
                "workday_operation_id was already used for a different operation"
            )
        return receipt
    # A new provider attempt naturally mints a new client operation UUID. The
    # packet ledger is the durable authority: an exact op+fingerprint already
    # recorded by an earlier claim is an idempotent replay, not permission to
    # repeat an ambiguous mutation.
    semantic = [
        receipt for receipt in all_receipts
        if receipt["op"] == op
        and receipt["operation_fingerprint"] == operation_fingerprint
    ]
    if semantic:
        return semantic[-1]
    if not all_receipts:
        if len(raw_ledger) >= WORKDAY_OPERATION_LEDGER_LIMIT:
            raise _conflict(
                "Workday operation capacity is exhausted for this consultation"
            )
        return None
    if len(raw_ledger) >= WORKDAY_OPERATION_LEDGER_LIMIT:
        raise _conflict(
            "Workday operation capacity is exhausted for this consultation"
        )
    return None


async def prepare_workday_consultation_operation_binding(
    session: AsyncSession,
    *,
    project: Project,
    task: Task,
    resident_incarnation_id: uuid.UUID | None,
    packet_id: uuid.UUID | None,
    workday_claim_id: uuid.UUID | None,
    operation_id: uuid.UUID | None,
) -> WorkdayConsultationOperationBinding | None:
    """Bind a managed operation to one exact live private packet."""

    task_context = task.context if isinstance(task.context, dict) else {}
    if task_context.get("_resident_session") is not True:
        if packet_id is not None or workday_claim_id is not None or operation_id is not None:
            raise _conflict("Workday consultation provenance is not valid for this task")
        return None
    resident_session_id = str(task_context.get("strategic_session_id") or "").strip()
    worker_generation = _nonnegative_int(
        task_context.get("_resident_worker_generation") or 1
    )
    if not resident_session_id or worker_generation is None or resident_incarnation_id is None:
        raise _conflict("Resident identity is unavailable for Workday mutation")

    from app.conversation_coordinator import (
        _WORKDAY_CLAIM_EXPIRES_CONTEXT_KEY,
        _WORKDAY_CLAIM_GENERATION_CONTEXT_KEY,
        _WORKDAY_CLAIM_ID_CONTEXT_KEY,
        _WORKDAY_CLAIM_INCARNATION_CONTEXT_KEY,
        _is_workday_consultation_packet,
        _workday_claim_expiry,
    )

    statement = select(ProjectMessage).where(
        ProjectMessage.project_id == project.id,
        ProjectMessage.status == "open",
        ProjectMessage.handling_state == MessageHandlingState.CLAIMED,
        ProjectMessage.handled_by_task_id == task.id,
        ProjectMessage.resident_session_id == resident_session_id,
    )
    if packet_id is not None:
        statement = statement.where(ProjectMessage.id == packet_id)
    rows = (await session.execute(statement.with_for_update())).scalars().all()
    packets = [
        row
        for row in rows
        if _is_workday_consultation_packet(
            row, resident_session_id=resident_session_id
        )
    ]
    if packet_id is None:
        if packets:
            raise _conflict(
                "Managed operation during a private Workday lease requires "
                "packet_id and workday_claim_id"
            )
        return None
    if operation_id is None:
        raise _conflict("Managed Workday operation requires workday_operation_id")
    if len(packets) != 1:
        raise _conflict("Workday consultation packet is unavailable")
    packet = packets[0]
    context = packet.context if isinstance(packet.context, dict) else {}
    expires_at = _workday_claim_expiry(
        context.get(_WORKDAY_CLAIM_EXPIRES_CONTEXT_KEY)
    )
    if (
        context.get(_WORKDAY_CLAIM_ID_CONTEXT_KEY) != str(workday_claim_id)
        or context.get(_WORKDAY_CLAIM_GENERATION_CONTEXT_KEY) != worker_generation
        or str(context.get(_WORKDAY_CLAIM_INCARNATION_CONTEXT_KEY))
        != str(resident_incarnation_id)
        or expires_at is None
        or expires_at <= _utcnow()
    ):
        raise _conflict("Workday consultation lease is expired or owned by another worker")

    reference = context.get("envelope")
    if not isinstance(reference, Mapping):
        raise _conflict("Workday consultation envelope is unavailable")
    from app.strategic_memory_envelope import (
        get_fenced_context_envelope_by_reference,
    )

    resolved = await get_fenced_context_envelope_by_reference(
        session,
        project_id=project.id,
        reference=reference,
    )
    snapshot_id = _uuid(reference.get("snapshot_id"))
    fast_claim_id = _uuid(context.get("claim_id"))
    if resolved is None or snapshot_id is None or fast_claim_id is None:
        raise _conflict("Workday consultation envelope is stale or revoked")
    snapshot = (
        await session.execute(
            select(StrategicContextEnvelopeSnapshot)
            .where(
                StrategicContextEnvelopeSnapshot.project_id == project.id,
                StrategicContextEnvelopeSnapshot.snapshot_id == snapshot_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        snapshot is None
        or snapshot.session_id != resident_session_id
        or snapshot.claim_id != fast_claim_id
        or snapshot.worker_generation != worker_generation
        or snapshot.worker_incarnation != resident_incarnation_id
        or snapshot.context_authority_generation
        != resolved.context_authority_generation
        or snapshot.privacy_generation != resolved.privacy_generation
        or snapshot.transcript_sequence != resolved.transcript_sequence
    ):
        raise _conflict("Workday consultation envelope identity does not match its lease")
    return WorkdayConsultationOperationBinding(
        packet=packet,
        task=task,
        packet_id=packet.id,
        workday_claim_id=workday_claim_id,
        operation_id=operation_id,
        fast_claim_id=fast_claim_id,
        task_id=task.id,
        resident_session_id=resident_session_id,
        worker_generation=worker_generation,
        worker_incarnation=resident_incarnation_id,
        input_revision=snapshot.input_revision,
        transcript_sequence=snapshot.transcript_sequence,
        envelope_snapshot_id=snapshot.snapshot_id,
        context_authority_generation_before=snapshot.context_authority_generation,
        privacy_generation=snapshot.privacy_generation,
    )


prepare_problem_model_workday_rebind = prepare_workday_consultation_operation_binding


async def prepare_bound_workday_operation(
    session: AsyncSession,
    *,
    project: Project,
    task: Task,
    resident_incarnation_id: uuid.UUID | None,
    packet_id: uuid.UUID | None,
    workday_claim_id: uuid.UUID | None,
    operation_id: uuid.UUID | None,
    op: str,
    data: object,
) -> tuple[
    BoundWorkdayOperation,
    dict[str, Any] | None,
]:
    """Prepare one exact lease and resolve any transport replay."""

    fingerprint = workday_operation_fingerprint(op, data)
    binding = await prepare_workday_consultation_operation_binding(
        session,
        project=project,
        task=task,
        resident_incarnation_id=resident_incarnation_id,
        packet_id=packet_id,
        workday_claim_id=workday_claim_id,
        operation_id=operation_id,
    )
    replay = (
        find_workday_operation_replay(
            binding,
            op=op,
            operation_fingerprint=fingerprint,
        )
        if op in WORKDAY_RECEIPT_BOUND_OPS
        else None
    )
    return (
        BoundWorkdayOperation(
            session=session,
            project=project,
            task=task,
            op=op,
            fingerprint=fingerprint,
            binding=binding,
        ),
        replay,
    )


def workday_operation_replay_response(receipt: Mapping[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status": "deferred" if receipt.get("effect") == "deferred" else "ok",
        "op": receipt["op"],
        "idempotent_replay": True,
    }
    entity_key = {
        "direction": "direction_id",
        "task": "task_id",
        "strategic_spawn_request": "request_id",
        "knowledge_document": "doc_id",
        "task_watch": "watch_id",
        "completion_debt": "debt_id",
        "acceptance_criterion": "criterion_key",
    }.get(str(receipt.get("entity_kind")))
    if receipt.get("op") == "cancel_task" and entity_key == "task_id":
        entity_key = "target_task_id"
    if entity_key is not None:
        body[entity_key] = receipt["entity_id"]
    if receipt.get("target_revision") is not None:
        body["revision"] = receipt["target_revision"]
    return body


async def rebind_problem_model_workday_packet(
    session: AsyncSession,
    *,
    binding: WorkdayProblemModelMutationBinding | None,
    project: Project,
    task: Task,
    op: str,
    problem_model: Mapping[str, Any],
    operation_fingerprint: str,
) -> None:
    """Flush one bound write, mint its current envelope, and swap atomically."""

    problem_model_bytes = canonical_json_bytes(dict(problem_model))
    await record_workday_consultation_operation(
        session,
        binding=binding,
        project=project,
        task=task,
        op=op,
        target_kind="problem_model",
        target_revision=problem_model.get("revision", 0),
        target_sha256=hashlib.sha256(problem_model_bytes).hexdigest(),
        operation_fingerprint=operation_fingerprint,
    )


async def record_workday_consultation_operation(
    session: AsyncSession,
    *,
    binding: WorkdayConsultationOperationBinding | None,
    project: Project,
    task: Task,
    op: str,
    target_kind: str | None = None,
    target_revision: str | int | None = None,
    target_sha256: str | None = None,
    entity_kind: str | None = None,
    entity_id: str | None = None,
    effect: str | None = None,
    operation_fingerprint: str,
) -> None:
    """Record one exact consultation operation and rebind if authority moved."""

    if binding is None:
        return
    if _SHA256_PATTERN.fullmatch(operation_fingerprint) is None:
        raise _conflict("Workday operation fingerprint is invalid")
    if task.id != binding.task_id or op not in WORKDAY_RECEIPT_BOUND_OPS:
        raise _conflict("Workday mutation binding does not match this operation")
    if op in _PROBLEM_MODEL_OPS:
        if (
            target_kind != "problem_model"
            or target_revision is None
            or target_sha256 is None
        ):
            raise _conflict("Problem-model operation receipt is incomplete")
    elif op in _STATE_OP_TARGET_KINDS:
        if (
            target_kind != _STATE_OP_TARGET_KINDS[op]
            or target_revision is None
            or target_sha256 is None
        ):
            raise _conflict("Managed-state operation receipt is incomplete")
    elif entity_kind not in _ENTITY_KINDS or not entity_id or effect not in {
        "created", "updated", "replayed", "deferred"
    }:
        raise _conflict("Managed entity operation receipt is incomplete")
    await session.flush()

    from app.strategic_memory_envelope import (
        ContextEnvelopeFenceError,
        build_and_store_context_envelope_snapshot,
        context_envelope_reference,
        get_or_create_project_memory_state,
        pin_fenced_context_envelope_for_packet,
    )

    state = await get_or_create_project_memory_state(session, project.id, lock=True)
    authority_after = state.context_authority_generation
    if (
        authority_after
        not in {
            binding.context_authority_generation_before,
            binding.context_authority_generation_before + 1,
        }
        or state.privacy_generation != binding.privacy_generation
    ):
        raise _conflict("Strategic authority or privacy changed during Workday mutation")
    snapshot_id = binding.envelope_snapshot_id
    context = dict(binding.packet.context or {})
    if authority_after != binding.context_authority_generation_before:
        from app.strategic_fast_live_work import _fast_live_work_authority

        decision_authority = await _fast_live_work_authority(
            session,
            project=project,
            resident_task_id=task.id,
        )
        try:
            fresh = await build_and_store_context_envelope_snapshot(
                session,
                project_id=project.id,
                session_id=binding.resident_session_id,
                claim_id=binding.fast_claim_id,
                worker_generation=binding.worker_generation,
                worker_incarnation=binding.worker_incarnation,
                input_revision=binding.input_revision,
                transcript_sequence=binding.transcript_sequence,
                decision_authority=decision_authority,
                expected_context_authority_generation=authority_after,
                expected_privacy_generation=binding.privacy_generation,
                retrieval_trace={"workday_self_mutation_rebind": True},
                memory_query=binding.packet.content,
            )
        except ContextEnvelopeFenceError as exc:
            raise _conflict(
                "Strategic authority changed during Workday envelope rebind"
            ) from exc
        pinned = await pin_fenced_context_envelope_for_packet(
            session,
            project_id=project.id,
            reference=context_envelope_reference(fresh),
        )
        if pinned is None:
            raise _conflict("Fresh Workday consultation envelope could not be pinned")
        snapshot_id = pinned.snapshot_id
        context["envelope"] = context_envelope_reference(pinned)

    raw_ledger = context.get(WORKDAY_OPERATION_LEDGER_CONTEXT_KEY)
    ledger = list(raw_ledger) if isinstance(raw_ledger, list) else []
    if any(_bounded_receipt(item) is None for item in ledger):
        raise _conflict("Workday operation history is invalid")
    prior = bounded_workday_mutation_receipts(
        {WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY: ledger},
        packet_id=binding.packet_id,
        task_id=binding.task_id,
        resident_session_id=binding.resident_session_id,
        worker_generation=binding.worker_generation,
        limit=WORKDAY_OPERATION_LEDGER_LIMIT,
    )
    receipt: dict[str, Any] = {
        "version": 1,
        "packet_id": str(binding.packet_id),
        "workday_claim_id": str(binding.workday_claim_id),
        "operation_id": str(binding.operation_id),
        "fast_claim_id": str(binding.fast_claim_id),
        "task_id": str(binding.task_id),
        "resident_session_id": binding.resident_session_id,
        "worker_generation": binding.worker_generation,
        "worker_incarnation": str(binding.worker_incarnation),
        "op": op,
        "operation_fingerprint": operation_fingerprint,
        "context_authority_generation_before": (
            binding.context_authority_generation_before
        ),
        "context_authority_generation_after": authority_after,
        "privacy_generation": binding.privacy_generation,
        "envelope_snapshot_id": str(snapshot_id),
        "recorded_at": _utcnow().isoformat(),
    }
    if op in _PROBLEM_MODEL_OPS or op in _STATE_OP_TARGET_KINDS:
        receipt.update(
            target_kind=target_kind,
            target_revision=target_revision,
            target_sha256=target_sha256,
        )
    else:
        receipt.update(
            entity_kind=entity_kind,
            entity_id=entity_id,
            effect=effect,
            accepted_at=receipt["recorded_at"],
        )
    combined = [*prior, receipt]
    context[WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY] = combined[-WORKDAY_MUTATION_RECEIPT_LIMIT:]
    context[WORKDAY_MUTATION_RECEIPTS_HAS_MORE_CONTEXT_KEY] = bool(
        workday_mutation_receipts_have_more(context)
        or len(combined) > WORKDAY_MUTATION_RECEIPT_LIMIT
    )
    if any(
        item.get("operation_id") == str(binding.operation_id) for item in ledger
    ):
        raise _conflict("workday_operation_id was already recorded")
    if len(ledger) >= WORKDAY_OPERATION_LEDGER_LIMIT:
        raise _conflict(
            "Workday operation capacity is exhausted for this consultation"
        )
    context[WORKDAY_OPERATION_LEDGER_CONTEXT_KEY] = [*ledger, receipt]
    binding.packet.context = context
    binding.packet.updated_at = _utcnow()
    session.add(binding.packet)
    await session.flush()
    current = await get_or_create_project_memory_state(session, project.id, lock=True)
    if (
        current.context_authority_generation != authority_after
        or current.privacy_generation != binding.privacy_generation
    ):
        raise _conflict("Strategic authority or privacy changed after Workday rebind")


__all__ = [
    "WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY",
    "WORKDAY_MUTATION_RECEIPTS_HAS_MORE_CONTEXT_KEY",
    "WORKDAY_OPERATION_LEDGER_CONTEXT_KEY",
    "WORKDAY_OPERATION_LEDGER_LIMIT",
    "WORKDAY_RECEIPT_BOUND_OPS",
    "BoundWorkdayOperation",
    "bounded_workday_mutation_receipts",
    "find_workday_operation_replay",
    "prepare_workday_consultation_operation_binding",
    "prepare_problem_model_workday_rebind",
    "prepare_bound_workday_operation",
    "record_workday_consultation_operation",
    "rebind_problem_model_workday_packet",
    "workday_mutation_receipts_have_more",
    "workday_operation_fingerprint",
    "workday_operation_replay_response",
    "workday_state_sha256",
]
