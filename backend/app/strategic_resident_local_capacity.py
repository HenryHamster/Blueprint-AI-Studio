"""Compatible local-runner readiness for Strategic Resident turns."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import LocalRunner, StrategicResident, Task, TaskStatus
from app.runner_eligibility import (
    _coerce_max_concurrent,
    _runner_agent_available,
    _runner_handles_task_type,
)
from app.runner_health_monitors import _RUNNER_LIVENESS_SECONDS
from app.runner_project_binding import runner_allowed_for_project
from app.strategic_resident_runner_policy import RESIDENT_TURN_TASK_TYPE
from app.time_utils import _utcnow


async def local_runner_available(
    session: AsyncSession,
    resident: StrategicResident,
) -> bool:
    cutoff = _utcnow() - timedelta(seconds=_RUNNER_LIVENESS_SECONDS)
    runners = (
        await session.execute(
            select(LocalRunner).where(
                runner_allowed_for_project(resident.project_id),
                LocalRunner.status == "active",
                LocalRunner.last_seen_at.is_not(None),
                LocalRunner.last_seen_at > cutoff,
            )
        )
    ).scalars().all()
    compatible = [
        runner
        for runner in runners
        if _runner_agent_available(runner)
        and _runner_handles_task_type(runner, RESIDENT_TURN_TASK_TYPE)
    ]
    if not compatible:
        return False
    from app.orchestrator_runner_dispatch import _load_runner_compute_job_assignments

    compute_jobs = await _load_runner_compute_job_assignments(
        session, [runner.id for runner in compatible]
    )
    vm_ids = [f"runner:{runner.id}" for runner in compatible]
    active_rows = (
        await session.execute(
            select(Task.vm_instance_id).where(
                Task.vm_instance_id.in_(vm_ids),
                Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
            )
        )
    ).all()
    active_by_vm = dict.fromkeys(vm_ids, 0)
    for (vm_id,) in active_rows:
        if vm_id in active_by_vm:
            active_by_vm[vm_id] += 1
    return any(
        (maximum := _coerce_max_concurrent((runner.capabilities or {}).get("max_concurrent")))
        is None
        or active_by_vm[f"runner:{runner.id}"]
        + len(compute_jobs.get(runner.id, ()))
        < maximum
        for runner in compatible
    )


__all__ = ["local_runner_available"]
