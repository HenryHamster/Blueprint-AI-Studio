"""Fenced creation of bounded managed Tasks by the Strategic Resident."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifact_refs import (
    RESIDENT_ARTIFACT_REF_GRANTS_KEY,
    resident_artifact_ref_grants,
)
from app.models import ArtifactRef, Task, TaskStatus
from app.payload_schemas import validate_internal_payload
from app.strategic_operation_authority_validation import (
    BoundedOperationResult,
    FencedOperationContext,
    FencedOperationUsageError,
)
from app.strategic_resident_inspection_receipt import TURN_CONTEXT_HASH_KEY

if TYPE_CHECKING:
    from app.strategic_resident_models import StrategicOperationReceipt


MANAGED_TASK_CREATE_SERVICE = "resident_managed_task_create"
MANAGED_TASK_CONTEXT_KEY = "_resident_managed_task"
_CAPABILITY_TASK_TYPES = {
    "task.code_change.create": "create",
    "task.debug.create": "debug",
}
_TITLE_MAX = 500
_PROMPT_MAX = 8_000
MAX_MANAGED_TASK_ARTIFACT_REFS = 16
MAX_MANAGED_TASK_SOURCE_EVIDENCE = 8


@dataclass(frozen=True)
class ManagedTaskCreatePlan:
    """Detached, single-row Task creation plan applied by the fenced executor."""

    task_id: uuid.UUID
    capability_id: str
    task_type: str
    title: str
    prompt: str
    target_task_id: str | None
    source_evidence: tuple[dict[str, str], ...]


class ManagedTaskCreateWriter:
    """Records at most one managed Task creation without exposing a DB session."""

    def __init__(
        self,
        receipt_id: uuid.UUID,
        *,
        authorized_capability: str,
    ) -> None:
        self._receipt_id = receipt_id
        self._authorized_capability = authorized_capability
        self._authorized_task_type = _CAPABILITY_TASK_TYPES.get(
            authorized_capability
        )
        self._plan: ManagedTaskCreatePlan | None = None

    def create(
        self,
        *,
        capability_id: str,
        task_type: str,
        title: str,
        prompt: str,
        target_task_id: object = None,
        source_evidence: object = (),
    ) -> uuid.UUID:
        if self._plan is not None:
            raise FencedOperationUsageError(
                "managed Task creation plan already contains a Task"
            )
        if (
            capability_id != self._authorized_capability
            or task_type != self._authorized_task_type
        ):
            raise FencedOperationUsageError(
                "managed Task creation is not authorized by the receipt capability"
            )
        clean_title = title.strip() if isinstance(title, str) else ""
        clean_prompt = prompt.strip() if isinstance(prompt, str) else ""
        if not clean_title or len(clean_title) > _TITLE_MAX:
            raise FencedOperationUsageError("managed Task title is invalid")
        if not clean_prompt or len(clean_prompt) > _PROMPT_MAX:
            raise FencedOperationUsageError("managed Task prompt is invalid")
        clean_target_task_id: str | None = None
        if task_type == "debug":
            try:
                clean_target_task_id = str(uuid.UUID(str(target_task_id)))
            except (TypeError, ValueError):
                raise FencedOperationUsageError(
                    "managed debug Task requires a target task_id"
                ) from None
        elif target_task_id is not None:
            raise FencedOperationUsageError(
                "managed code-change Task cannot declare a target task_id"
            )
        if not isinstance(source_evidence, (list, tuple)):
            raise FencedOperationUsageError("managed Task source evidence is invalid")
        if len(source_evidence) > MAX_MANAGED_TASK_SOURCE_EVIDENCE:
            raise FencedOperationUsageError(
                "managed Task source evidence exceeds the bounded handoff limit"
            )
        exact_evidence: list[dict[str, str]] = []
        for item in source_evidence:
            if not hasattr(item, "get"):
                raise FencedOperationUsageError("managed Task source evidence is invalid")
            projected = {
                key: str(item.get(key) or "")
                for key in (
                    "kind",
                    "project_id",
                    "source_id",
                    "revision",
                    "checksum",
                    "inspected_at",
                )
            }
            if any(not value for value in projected.values()):
                raise FencedOperationUsageError("managed Task source evidence is invalid")
            exact_evidence.append(projected)
        if (
            clean_target_task_id is not None
            and not any(
                item["kind"] == "task"
                and item["source_id"] == clean_target_task_id
                for item in exact_evidence
            )
        ):
            raise FencedOperationUsageError(
                "managed debug Task evidence does not match its target task_id"
            )
        task_id = uuid.uuid5(self._receipt_id, "strategic-resident-managed-task")
        self._plan = ManagedTaskCreatePlan(
            task_id=task_id,
            capability_id=capability_id,
            task_type=task_type,
            title=clean_title,
            prompt=clean_prompt,
            target_task_id=clean_target_task_id,
            source_evidence=tuple(exact_evidence),
        )
        return task_id

    def validated_plan(self) -> ManagedTaskCreatePlan | None:
        return self._plan


def managed_task_create_operation_kind(capability_id: str) -> str | None:
    if capability_id in _CAPABILITY_TASK_TYPES:
        return "create_managed_task"
    return None


def managed_task_create_callback(capability_id: str):
    task_type = _CAPABILITY_TASK_TYPES.get(capability_id)
    if task_type is None:
        return None

    def _create(context: FencedOperationContext) -> BoundedOperationResult:
        task_id = context.managed_task_writer.create(
            capability_id=capability_id,
            task_type=task_type,
            title=str(context.request_payload["title"]),
            prompt=str(context.request_payload["prompt"]),
            target_task_id=context.request_payload.get("task_id"),
            source_evidence=context.request_payload.get("_source_evidence", ()),
        )
        return BoundedOperationResult(
            operations=[
                {
                    "operation_id": context.operation_id,
                    "capability": capability_id,
                    "status": "applied",
                    "summary": f"Queued managed Task {task_id}.",
                }
            ],
            facts=[
                {
                    "kind": "managed_task_created",
                    "task_id": str(task_id),
                    "task_type": task_type,
                    "status": TaskStatus.QUEUED.value,
                }
            ],
        )

    return _create


async def materialize_managed_task(
    session: AsyncSession,
    *,
    plan: ManagedTaskCreatePlan,
    turn_task: Task,
    receipt: StrategicOperationReceipt,
) -> Task:
    """Validate frozen source grants and insert exactly one immediately queued Task."""

    grant_ids = resident_artifact_ref_grants(turn_task)
    if len(grant_ids) > MAX_MANAGED_TASK_ARTIFACT_REFS:
        raise FencedOperationUsageError(
            "managed Task ArtifactRef grants exceed the bounded handoff limit"
        )
    refs = []
    if grant_ids:
        refs = list(
            (
                await session.execute(
                    select(ArtifactRef).where(
                        ArtifactRef.project_id == turn_task.project_id,
                        ArtifactRef.id.in_(grant_ids),
                    )
                )
            ).scalars()
        )
        if {ref.id for ref in refs} != set(grant_ids):
            raise FencedOperationUsageError(
                "frozen Resident turn contains an invalid ArtifactRef grant"
            )
    source_refs = [
        {
            "artifact_ref_id": str(ref.id),
            "tool_id": str(ref.tool_id),
            "kind": ref.kind,
            "revision": ref.revision,
            "checksum": ref.checksum,
            "author": ref.author_user_id,
            "timestamp": ref.created_at.isoformat(),
            "retrieval": {
                "href": (
                    "/api/agent/resident-turn/artifact"
                    f"?artifact_ref_id={ref.id}"
                )
            },
        }
        for ref in sorted(refs, key=lambda item: str(item.id))
    ]
    turn_payload = turn_task.first_payload or {}
    turn_context = turn_task.context if isinstance(turn_task.context, dict) else {}
    context_hash = str(turn_context.get(TURN_CONTEXT_HASH_KEY) or "")
    if not context_hash:
        raise FencedOperationUsageError(
            "managed Task creation requires frozen Resident turn context"
        )
    source_lineage = {
        "source_turn_task_id": str(turn_task.id),
        "authority_generation": receipt.authority_generation,
        "operation_id": receipt.operation_id,
        "receipt_id": str(receipt.id),
        "capability_id": plan.capability_id,
        "context_hash": context_hash,
        "message_sequence_start": turn_payload.get("message_sequence_start"),
        "message_sequence_end": turn_payload.get("message_sequence_end"),
        "artifact_refs": source_refs,
        "evidence": list(plan.source_evidence),
        "target_task_id": plan.target_task_id,
    }
    payload_data = {
        "type": plan.task_type,
        "prompt": plan.prompt,
        "resident_source_lineage": source_lineage,
    }
    if plan.task_type == "debug":
        payload_data.update(
            {
                "failed_task_title": plan.title,
                "failed_task_prompt": plan.prompt,
                "failure_context": plan.prompt,
            }
        )
    payload = validate_internal_payload(
        payload_data,
        f"strategic_resident:{plan.capability_id}",
    )
    context = {
        MANAGED_TASK_CONTEXT_KEY: source_lineage,
        RESIDENT_ARTIFACT_REF_GRANTS_KEY: [str(ref.id) for ref in refs],
    }
    task = Task(
        id=plan.task_id,
        project_id=turn_task.project_id,
        title=plan.title,
        description=plan.prompt[:500],
        parent_task_id=None,
        payloads=[payload],
        context=context,
        status=TaskStatus.QUEUED,
        priority=80,
        vm_instance_id=f"agent-vm-{plan.task_id.hex}",
        created_by="STRATEGIC_AGENT",
        idempotency_key=f"resident-receipt:{receipt.id}",
        strategic_operation_receipt_id=receipt.id,
    )
    session.add(task)
    await session.flush()
    return task


__all__ = [
    "MANAGED_TASK_CONTEXT_KEY",
    "MANAGED_TASK_CREATE_SERVICE",
    "MAX_MANAGED_TASK_ARTIFACT_REFS",
    "MAX_MANAGED_TASK_SOURCE_EVIDENCE",
    "ManagedTaskCreatePlan",
    "ManagedTaskCreateWriter",
    "managed_task_create_callback",
    "managed_task_create_operation_kind",
    "materialize_managed_task",
]
