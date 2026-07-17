"""Provider-route and cooldown readiness for Strategic Resident turns."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from app import shared_state as _shared_state
from app.max_plan_pool import (
    db_pool_wide_rate_limit_cooldown,
    infer_provider_for_model,
    pool_wide_rate_limit_cooldown,
    provider_selection_mode,
    resolve_task_provider_preference,
)
from app.models import Project, StrategicResident
from app.strategic_resident_provider_policy import (
    ResidentProviderPolicyHold,
    resolve_resident_provider_routing,
    validate_resident_model_provider,
)
from app.strategic_resident_runner_policy import RESIDENT_TURN_TASK_TYPE


@dataclass(frozen=True)
class _ProviderCandidates:
    providers: tuple[str, ...] = ()
    model: str | None = None
    confirmed_route_hold: bool = False


def _provider_candidates(resident: StrategicResident) -> _ProviderCandidates:
    policy = resident.provider_policy if isinstance(resident.provider_policy, dict) else {}
    failover = resident.failover_policy if isinstance(resident.failover_policy, dict) else {}
    task = SimpleNamespace(payloads=[{"type": RESIDENT_TURN_TASK_TYPE}], context={})
    project_providers = resolve_task_provider_preference(
        task, _shared_state.runtime_config
    )
    try:
        routing = resolve_resident_provider_routing(
            SimpleNamespace(
                payloads=[
                    {
                        "type": RESIDENT_TURN_TASK_TYPE,
                        "resident_provider_policy": policy,
                        "resident_failover_policy": failover,
                    }
                ],
                context={"_resident_no_silent_provider_downgrade": True},
            ),
            project_provider_preference=project_providers,
            allow_cross_provider_fallback=provider_selection_mode(
                _shared_state.runtime_config
            )
            in {"balanced", "preferred"},
        )
        validate_resident_model_provider(
            routing,
            lambda model: infer_provider_for_model(
                model, _shared_state.runtime_config
            ),
        )
    except ResidentProviderPolicyHold:
        return _ProviderCandidates(confirmed_route_hold=True)
    if routing is None or not routing.provider_preference:
        return _ProviderCandidates(confirmed_route_hold=True)
    return _ProviderCandidates(routing.provider_preference, routing.primary_model)


def provider_route_confirmed_unavailable(resident: StrategicResident) -> bool:
    return _provider_candidates(resident).confirmed_route_hold


async def provider_pool_confirmed_rate_limited(
    resident: StrategicResident,
    project: Project,
) -> bool:
    candidates = _provider_candidates(resident)
    if candidates.confirmed_route_hold:
        return False
    providers, model = candidates.providers, candidates.model
    scoped = {
        "user_id": project.user_id,
        "providers": list(providers),
        "model": model,
        "project_id": str(project.id),
    }
    # Process-local cooldown/cap state is only a cheap first pass. A DB-backed
    # cooldown confirms it; a project-account cap deliberately remains unknown
    # until durable project-scoped usage reconciliation is available.
    if not pool_wide_rate_limit_cooldown(**scoped):
        return False
    return await db_pool_wide_rate_limit_cooldown(
        user_id=project.user_id,
        providers=list(providers),
        model=model,
    )


__all__ = [
    "provider_pool_confirmed_rate_limited",
    "provider_route_confirmed_unavailable",
]
