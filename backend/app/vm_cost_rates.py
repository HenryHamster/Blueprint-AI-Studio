"""Single source of truth for VM cost rates (on-demand and spot) by machine type."""

from __future__ import annotations

import logging
from typing import Tuple

logger = logging.getLogger(__name__)

DEFAULT_MACHINE_TYPE = "e2-standard-2"

# Pricing per machine type: (on_demand_hourly, spot_hourly)
# Updated 2025-02: GCP pricing for standard E2 machines in us-central1.
# Rates beyond the default are scaled to keep the existing common-type behavior
# stable while making non-default machine types visible and distinct.
VM_COST_RATES: dict[str, Tuple[float, float]] = {
    "e2-standard-2": (0.067, 0.020),
    "e2-standard-4": (0.134, 0.040),
    "e2-standard-8": (0.268, 0.080),
    "e2-standard-16": (0.536, 0.160),
    "e2-standard-32": (1.072, 0.320),
}


def _resolve_rate_entry(machine_type: str | None) -> tuple[str, Tuple[float, float]]:
    if machine_type in VM_COST_RATES:
        return machine_type, VM_COST_RATES[machine_type]

    fallback_type = DEFAULT_MACHINE_TYPE
    logger.warning(
        "Unknown VM machine type %r; falling back to %s pricing",
        machine_type,
        fallback_type,
    )
    return fallback_type, VM_COST_RATES[fallback_type]


def get_vm_rate_per_second(
    machine_type: str | None = None, spot: bool = False
) -> float:
    """Return the per-second VM cost rate for the given machine type.

    Args:
        machine_type: GCP machine type (e.g., "e2-standard-2"). If None, uses the
                      configured default from the MACHINE_TYPE env var.
        spot: If True, return spot rate; otherwise return on-demand rate.

    Returns:
        Rate in $/second.
    """
    if machine_type is None:
        from app.compute import MACHINE_TYPE

        machine_type = MACHINE_TYPE

    _, hourly_rate = _resolve_rate_entry(machine_type)
    selected_rate = hourly_rate[1] if spot else hourly_rate[0]
    return selected_rate / 3600


def fleet_provisioning_is_spot() -> bool:
    """True iff the fleet boots agent VMs with spot/preemptible pricing.

    Pool VMs are not per-task-typed — they boot on the fleet-wide
    ``MACHINE_TYPE`` + ``VM_PROVISIONING_MODEL`` (see app.compute), so idle-cost
    accounting prices them at the fleet rate rather than a per-task rate.
    """
    from app.compute import VM_PROVISIONING_MODEL

    return VM_PROVISIONING_MODEL == "SPOT"


def get_fleet_vm_rate_per_second() -> float:
    """Per-second VM rate for the fleet default machine type + provisioning model.

    Used by warm-pool idle-cost accounting: an idle pool member bills GCE
    wall-clock at the fleet's configured machine type and spot/on-demand mode,
    not at any task-specific rate (it carries no task).
    """
    from app.compute import MACHINE_TYPE

    return get_vm_rate_per_second(MACHINE_TYPE, spot=fleet_provisioning_is_spot())


def idle_cost_usd(
    idle_seconds: float,
    *,
    machine_type: str | None = None,
    spot: bool | None = None,
) -> float:
    """Dollar cost of an idle VM sitting for ``idle_seconds``.

    ``machine_type`` / ``spot`` default to the fleet configuration (the regime
    pool VMs actually boot under). Non-positive durations cost nothing.
    """
    if idle_seconds <= 0:
        return 0.0
    if spot is None:
        spot = fleet_provisioning_is_spot()
    return idle_seconds * get_vm_rate_per_second(machine_type, spot=spot)
