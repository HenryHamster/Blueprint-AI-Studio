"""Bounded non-local scheduler readiness for Strategic Resident turns."""

from __future__ import annotations

from sqlalchemy import case, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app import shared_state as _shared_state
from app.models import Project, StrategicResident, Task, TaskStatus
from app.resource_envelope import resolve_project_vm_cap


def resource_gate_confirmed_unavailable(
    resident: StrategicResident,
    project: Project,
) -> bool:
    recovery = resident.recovery_state if isinstance(resident.recovery_state, dict) else {}
    authoritative_hold = bool(
        recovery.get("authority_generation") == resident.authority_generation
        and recovery.get("status")
        in {"capacity_hold", "provider_hold", "spend_hold", "budget_hold"}
    )
    return bool(
        project.paused
        or authoritative_hold
    )


async def cloud_capacity_confirmed_unavailable(
    session: AsyncSession,
    resident: StrategicResident,
    project: Project,
) -> bool:
    raw_cap = _shared_state.runtime_config.get("max_concurrent_vms")
    if not isinstance(raw_cap, int) or isinstance(raw_cap, bool):
        return False
    if raw_cap <= 0:
        return True
    project_cap = resolve_project_vm_cap(project)
    global_count, project_count = (
        await session.execute(
            select(
                func.count(Task.id),
                func.sum(case((Task.project_id == resident.project_id, 1), else_=0)),
            ).where(
                Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
                # Null dispatch identity or machine type is ambiguous: it may be
                # a local pin or Cloud Run job before its sentinel is stamped.
                # Only durable facts that prove GCE ownership may produce a
                # user-visible waiting state; ambiguity deliberately fails open.
                Task.vm_instance_id.is_not(None),
                ~Task.vm_instance_id.like("runner:%"),
                Task.vm_machine_type.is_not(None),
                ~Task.vm_machine_type.like("cloud-run-%"),
            )
        )
    ).one()
    if int(global_count or 0) >= raw_cap:
        return True
    return bool(project_cap is not None and int(project_count or 0) >= project_cap)


__all__ = [
    "cloud_capacity_confirmed_unavailable",
    "resource_gate_confirmed_unavailable",
]
