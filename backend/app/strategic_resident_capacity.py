"""Compatibility-aware runner capacity for authoritative Resident turns."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, StrategicResident
from app.strategic_resident_runner_policy import (
    runner_policy_requires_local,
)
from app.strategic_resident_cloud_capacity import (
    cloud_capacity_confirmed_unavailable,
    resource_gate_confirmed_unavailable,
)
from app.strategic_resident_local_capacity import local_runner_available
from app.strategic_resident_provider_capacity import (
    provider_pool_confirmed_rate_limited,
    provider_route_confirmed_unavailable,
)


def requires_local_runner(resident: StrategicResident) -> bool:
    policy = resident.runner_policy if isinstance(resident.runner_policy, dict) else {}
    return runner_policy_requires_local(policy)


def _allows_local_fallback(resident: StrategicResident) -> bool:
    policy = resident.runner_policy if isinstance(resident.runner_policy, dict) else {}
    if requires_local_runner(resident):
        return True
    if policy.get("allow_local_fallback") is False:
        return False
    return policy.get("mode") not in {"cloud", "cloud_required"}


async def waiting_for_required_runner(
    session: AsyncSession,
    resident: StrategicResident,
) -> bool:
    """Project confirmed scheduler unavailability without network calls.

    Unknown or stale readiness fails open: durable message acceptance remains
    queued/coalesced unless a current scheduler signal proves it must wait.
    """
    if requires_local_runner(resident):
        return not await local_runner_available(session, resident)
    project = await session.get(Project, resident.project_id)
    if project is None:
        return False
    if resource_gate_confirmed_unavailable(resident, project):
        return True
    if provider_route_confirmed_unavailable(resident):
        return True
    if await provider_pool_confirmed_rate_limited(resident, project):
        return True
    if not await cloud_capacity_confirmed_unavailable(
        session, resident, project
    ):
        return False
    if _allows_local_fallback(resident):
        return not await local_runner_available(session, resident)
    return True


__all__ = ["requires_local_runner", "waiting_for_required_runner"]
