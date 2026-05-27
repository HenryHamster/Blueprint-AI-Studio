"""Single source of truth for VM cost rates (on-demand and spot) by machine type."""

from typing import Tuple

# Pricing per machine type: (on_demand_hourly, spot_hourly)
# Updated 2025-02: GCP pricing for standard machines in us-central1.
VM_COST_RATES: dict[str, Tuple[float, float]] = {
    "e2-standard-2": (0.067, 0.020),
}


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

    hourly_rate = VM_COST_RATES.get(machine_type, VM_COST_RATES["e2-standard-2"])
    selected_rate = hourly_rate[1] if spot else hourly_rate[0]
    return selected_rate / 3600
