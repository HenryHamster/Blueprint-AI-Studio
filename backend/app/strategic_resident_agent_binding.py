"""Task-scoped agent binding for authoritative Resident endpoints."""

from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Project, StrategicResident, Task, TaskStatus
from app.strategic_resident_turns import (
    MUTATION_AUTHORIZED_CONTEXT_KEY,
    SHADOW_CONTEXT_KEY,
    STRATEGIC_RESIDENT_TURN_TASK_TYPE,
)


RESIDENT_CLAIM_NONCE_CONTEXT_KEY = "_strategic_resident_claim_nonce"
RESIDENT_CLAIM_ATTEMPT_CONTEXT_KEY = "_strategic_resident_claim_attempt_jti"


class ResidentAgentBindingError(RuntimeError):
    """The presented agent token is not bound to this Resident task."""


class ResidentAgentAuthorityError(RuntimeError):
    """The bound task is no longer the live authoritative Resident turn."""


class ResidentAgentTaskNotFound(RuntimeError):
    """The task named by a Resident agent token no longer exists."""


@dataclass(frozen=True)
class ResidentAgentBinding:
    task_id: uuid.UUID
    project_id: uuid.UUID
    vm_instance_id: str
    task_type: str
    claim_nonce: str


def validate_resident_agent_binding(
    task: Task,
    binding: ResidentAgentBinding,
    *,
    allowed_statuses: frozenset[TaskStatus],
) -> None:
    """Fail closed unless token claims match the exact authoritative task."""

    payload = task.first_payload if isinstance(task.first_payload, dict) else {}
    context = task.context if isinstance(task.context, dict) else {}
    if task.id != binding.task_id:
        raise ResidentAgentBindingError("agent token task mismatch")
    if task.project_id != binding.project_id:
        raise ResidentAgentBindingError("agent token project mismatch")
    if (
        not binding.vm_instance_id
        or not task.vm_instance_id
        or task.vm_instance_id != binding.vm_instance_id
    ):
        raise ResidentAgentBindingError("agent token VM mismatch")
    if (
        binding.task_type != STRATEGIC_RESIDENT_TURN_TASK_TYPE
        or payload.get("type") != STRATEGIC_RESIDENT_TURN_TASK_TYPE
        or payload.get("shadow_only") is not False
        or context.get(SHADOW_CONTEXT_KEY) is not False
        or context.get(MUTATION_AUTHORIZED_CONTEXT_KEY) is not True
    ):
        raise ResidentAgentBindingError("authoritative Resident task scope required")
    if task.status not in allowed_statuses:
        raise ResidentAgentBindingError("Resident task status is not authorized")
    persisted_nonce = str(context.get(RESIDENT_CLAIM_NONCE_CONTEXT_KEY) or "")
    if (
        not persisted_nonce
        or not binding.claim_nonce
        or not hmac.compare_digest(binding.claim_nonce, persisted_nonce)
    ):
        raise ResidentAgentBindingError("Resident claim nonce is stale or missing")


async def load_live_resident_agent_task_locked(
    session: AsyncSession,
    binding: ResidentAgentBinding,
    *,
    allowed_statuses: frozenset[TaskStatus],
) -> Task:
    """Lock Project -> Resident -> Task and validate the complete live binding."""

    project = (
        await session.execute(
            select(Project)
            .where(Project.id == binding.project_id)
            .with_for_update(key_share=True)
        )
    ).scalar_one_or_none()
    resident = (
        await session.execute(
            select(StrategicResident)
            .where(StrategicResident.project_id == binding.project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if project is None or resident is None:
        raise ResidentAgentAuthorityError("Resident authority is stale")
    task = (
        await session.execute(
            select(Task)
            .where(Task.id == binding.task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if task is None:
        raise ResidentAgentTaskNotFound("Resident turn not found")
    payload = task.first_payload if isinstance(task.first_payload, dict) else {}
    if (
        task.project_id != binding.project_id
        or resident.engine_owner != "strategic_resident"
        or resident.active_turn_task_id != task.id
        or int(payload.get("authority_generation") or -1)
        != int(resident.authority_generation)
    ):
        raise ResidentAgentAuthorityError("Resident turn authority is stale")
    validate_resident_agent_binding(
        task,
        binding,
        allowed_statuses=allowed_statuses,
    )
    return task


__all__ = [
    "RESIDENT_CLAIM_ATTEMPT_CONTEXT_KEY",
    "RESIDENT_CLAIM_NONCE_CONTEXT_KEY",
    "ResidentAgentAuthorityError",
    "ResidentAgentBinding",
    "ResidentAgentBindingError",
    "ResidentAgentTaskNotFound",
    "load_live_resident_agent_task_locked",
    "validate_resident_agent_binding",
]
