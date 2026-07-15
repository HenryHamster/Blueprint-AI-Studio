"""Single source of truth for VM cost rates (on-demand and spot) by machine type."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Tuple

from sqlalchemy import and_, case, func, or_

logger = logging.getLogger(__name__)

DEFAULT_MACHINE_TYPE = "e2-standard-2"

# Pricing per machine type: (on_demand_hourly, spot_hourly)
# Updated 2026-07: GCP pricing for standard E2/C2D machines in us-central1.
# Rates beyond the default are scaled to keep the existing common-type behavior
# stable while making non-default machine types visible and distinct.
VM_COST_RATES: dict[str, Tuple[float, float]] = {
    "e2-standard-2": (0.067, 0.020),
    "e2-standard-4": (0.134, 0.040),
    "e2-standard-8": (0.268, 0.080),
    "e2-standard-16": (0.536, 0.160),
    "e2-standard-32": (1.072, 0.320),
    # Capacity fallback for region-wide E2/N2 stockouts. Google Cloud's Iowa
    # price sheet lists $0.090798 on-demand and $0.023988 current Spot hourly.
    "c2d-standard-2": (0.090798, 0.023988),
}


def supported_machine_types() -> tuple[str, ...]:
    """Return the explicitly priced machine types the platform accepts."""
    return tuple(sorted(VM_COST_RATES))


def is_supported_machine_type(machine_type: str | None) -> bool:
    """True when ``machine_type`` has an explicit pricing entry."""
    return machine_type in VM_COST_RATES


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

    # Cloud Run sentinel machine types use a different rate table; spot has no
    # meaning for Cloud Run (there is no spot/preemptible concept there).
    if machine_type and machine_type.startswith("cloud-run-"):
        return get_cloud_run_rate_for_machine_type(machine_type)

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


# ---------------------------------------------------------------------------
# Single-GPU accelerator FACTS — ADDITIVE & INERT.
#
# These structures are deliberately SEPARATE from ``VM_COST_RATES`` so the
# CPU-only create gate (``supported_machine_types`` / ``is_supported_machine_type``)
# is unchanged: GPU machine types are NOT priced through ``VM_COST_RATES`` and
# therefore still report as unsupported until a later subtask wires GPU
# acceptance through the new accessor / gate below.
#
# Facts (machine/accelerator pairings, GCP type strings, prices) come from the
# gce-gpu-provisioning knowledge document; do not edit without re-grounding.
# ---------------------------------------------------------------------------

# N1 base machine pricing for guest-accelerator GPU shapes
# (on_demand_hourly, spot_hourly). For N1 GPU VMs the total cost is the base
# machine rate PLUS a separate per-GPU surcharge (kept as distinct components).
GPU_BASE_MACHINE_RATES: dict[str, Tuple[float, float]] = {
    "n1-standard-1": (0.04749975, 0.01381125),
}

# Per-GPU surcharge for N1 guest-accelerator shapes (on_demand_hourly,
# spot_hourly), keyed by GCP accelerator type string.
GPU_ACCELERATOR_SURCHARGE_RATES: dict[str, Tuple[float, float]] = {
    "nvidia-tesla-t4": (0.35, 0.161),
    "nvidia-tesla-v100": (2.48, 1.0912),
}

# Bundled machine pricing for G2 shapes: the L4 GPU is built into the machine
# price, so there is no separate surcharge (on_demand_hourly, spot_hourly).
GPU_BUNDLED_MACHINE_RATES: dict[str, Tuple[float, float]] = {
    "g2-standard-4": (0.70683228, 0.058736),
}


@dataclass(frozen=True)
class AcceleratorSpec:
    """A supported single-GPU accelerator and how it attaches to a VM."""

    name: str  # canonical short name, e.g. "T4"
    gcp_type: str  # GCP accelerator type string, e.g. "nvidia-tesla-t4"
    machine_family: str  # "n1" (guest accelerator) or "g2" (built-in)
    machine_type: str  # canonical single-GPU machine type
    bundled: bool  # True when the machine price already includes the GPU (G2)


# The supported-accelerator gate as DATA. A100 is deliberately ABSENT — its
# quota is 0 (see the knowledge doc). Keyed by canonical short name.
SUPPORTED_ACCELERATORS: dict[str, AcceleratorSpec] = {
    "T4": AcceleratorSpec("T4", "nvidia-tesla-t4", "n1", "n1-standard-1", False),
    "L4": AcceleratorSpec("L4", "nvidia-l4", "g2", "g2-standard-4", True),
    "V100": AcceleratorSpec("V100", "nvidia-tesla-v100", "n1", "n1-standard-1", False),
}

# Reverse lookup so requests may name an accelerator by its GCP type string.
_ACCELERATOR_BY_GCP_TYPE: dict[str, AcceleratorSpec] = {
    spec.gcp_type: spec for spec in SUPPORTED_ACCELERATORS.values()
}


class AcceleratorRejection(ValueError):
    """Raised when an accelerator request is not a supported single-GPU shape."""


def resolve_accelerator_spec(accelerator: str | None) -> AcceleratorSpec | None:
    """Look up an ``AcceleratorSpec`` by short name or GCP type string.

    Case-insensitive on the short name. Returns ``None`` for unknown names
    (callers that need a rejection reason should use
    :func:`validate_accelerator_request`).
    """
    if accelerator is None:
        return None
    spec = SUPPORTED_ACCELERATORS.get(accelerator.upper())
    if spec is not None:
        return spec
    return _ACCELERATOR_BY_GCP_TYPE.get(accelerator)


def validate_accelerator_request(
    accelerator: str, count: int = 1
) -> Tuple[AcceleratorSpec | None, str | None]:
    """Validate a single-GPU accelerator request.

    Returns ``(spec, None)`` when the request is a supported single-GPU shape,
    or ``(None, reason)`` with a clear rejection message for unsupported
    accelerators (e.g. A100, which is out of quota), counts other than 1, or
    unknown accelerator names.
    """
    spec = resolve_accelerator_spec(accelerator)
    if spec is None:
        return None, (
            f"unsupported accelerator {accelerator!r}: only "
            f"{sorted(SUPPORTED_ACCELERATORS)} are supported "
            "(A100 is excluded — quota is 0)"
        )
    if count != 1:
        return None, (
            f"unsupported accelerator count {count!r} for {spec.name}: "
            "only single-GPU (count=1) requests are supported"
        )
    return spec, None


def validate_compute_request(
    accelerator: str | None,
    machine_type: str | None,
    count: int = 1,
) -> Tuple[AcceleratorSpec | None, str | None]:
    """Validate a (machine_type, optional accelerator) compute request.

    Three outcomes:
      * ``(None, None)`` — a valid CPU request (no accelerator, priced machine type).
      * ``(spec, None)`` — a valid single-GPU request (supported accelerator paired
        with a matching, priced GPU machine type).
      * ``(None, reason)`` — a rejection with a clear message (unsupported/over-quota
        accelerator like A100, count != 1, unknown accelerator, or a
        GPU/machine-family mismatch).

    This does NOT loosen the CPU gate: when ``accelerator`` is ``None`` it uses
    the default machine type only when absent, and still requires the resolved
    CPU machine type to be explicitly priced.
    """
    if accelerator is None:
        machine_type = resolve_compute_machine_type(
            accelerator=accelerator,
            machine_type=machine_type,
        )
        if not is_supported_machine_type(machine_type):
            return None, f"unsupported machine_type: {machine_type}"
        return None, None

    spec, reason = validate_accelerator_request(accelerator, count)
    if reason is not None:
        return None, reason
    assert spec is not None  # narrowed by validate_accelerator_request

    # The machine type must match the accelerator's machine family AND be priced.
    # GPU_BUNDLED_MACHINE_RATES holds only G2 (built-in L4) shapes;
    # GPU_BASE_MACHINE_RATES holds only N1 (guest-accelerator T4/V100) shapes — so
    # this single membership check rejects a GPU/machine-family mismatch (e.g. T4
    # with g2-standard-4, or L4 with n1-standard-1) as well as an unpriced shape.
    machine_type = resolve_compute_machine_type(
        accelerator=accelerator,
        machine_type=machine_type,
    )
    if spec.bundled:
        if machine_type not in GPU_BUNDLED_MACHINE_RATES:
            return None, (
                f"machine_type {machine_type!r} is not a valid single-GPU shape for "
                f"{spec.name}; expected one of {sorted(GPU_BUNDLED_MACHINE_RATES)}"
            )
    else:
        if machine_type not in GPU_BASE_MACHINE_RATES:
            return None, (
                f"machine_type {machine_type!r} is not a valid single-GPU shape for "
                f"{spec.name}; expected one of {sorted(GPU_BASE_MACHINE_RATES)}"
            )
    return spec, None


def resolve_compute_machine_type(
    *,
    accelerator: str | None,
    machine_type: str | None,
) -> str:
    """Return the machine type that should be validated, priced, and persisted.

    CPU requests with no explicit machine type keep the historical default. A
    supported accelerator with no explicit machine type resolves to that
    accelerator's canonical single-GPU shape. Explicit machine types are returned
    unchanged so a family mismatch is rejected by validation, never overwritten.
    """
    if machine_type is not None:
        return machine_type
    spec = resolve_accelerator_spec(accelerator)
    if spec is not None:
        return spec.machine_type
    return DEFAULT_MACHINE_TYPE


# ---------------------------------------------------------------------------
# Cloud Run Jobs cost rates (separate from VM_COST_RATES so supported_machine_types()
# and is_supported_machine_type() are unaffected).
#
# Approximate us-central1 list prices (2025):
#   CPU: $0.000024/vCPU-s (on-demand)
#   Memory: $0.0000025/GiB-s (on-demand)
# ---------------------------------------------------------------------------

# (cpu_count, memory_gib) for each Cloud Run machine type sentinel.
_CLOUD_RUN_SHAPES: dict[str, tuple[float, float]] = {
    "cloud-run-1vcpu-2gib": (1.0, 2.0),
    "cloud-run-2vcpu-4gib": (2.0, 4.0),
}

_CLOUD_RUN_CPU_RATE_PER_VCPU_SECOND = 0.000024  # USD/vCPU-s
_CLOUD_RUN_MEM_RATE_PER_GIB_SECOND = 0.0000025  # USD/GiB-s


def get_cloud_run_rate_per_second(
    cpu_count: float = 1.0, memory_gib: float = 2.0
) -> float:
    """Return the per-second Cloud Run Jobs cost for the given shape.

    Args:
        cpu_count: Number of vCPUs allocated to the execution.
        memory_gib: Memory allocated in GiB.

    Returns:
        Rate in USD/second.
    """
    return (
        cpu_count * _CLOUD_RUN_CPU_RATE_PER_VCPU_SECOND
        + memory_gib * _CLOUD_RUN_MEM_RATE_PER_GIB_SECOND
    )


def get_cloud_run_rate_for_machine_type(machine_type: str) -> float:
    """Return the per-second Cloud Run cost for a known sentinel machine type.

    Falls back to the standard 1vCPU/2GiB shape for unknown sentinels.
    """
    cpu, mem = _CLOUD_RUN_SHAPES.get(machine_type, (1.0, 2.0))
    return get_cloud_run_rate_per_second(cpu, mem)


def get_compute_rate_per_second(
    machine_type: str | None = None,
    accelerator: str | None = None,
    count: int = 1,
    spot: bool = False,
) -> float:
    """Per-second cost rate for a (machine type, optional accelerator) request.

    When ``accelerator`` is ``None`` this falls through to
    :func:`get_vm_rate_per_second`, so existing CPU (e2) callers are unchanged.

    When an accelerator is requested, the rate is:
      * N1 guest-accelerator shapes (T4/V100): base machine rate + GPU surcharge.
      * G2 bundled shapes (L4): the machine rate, which already includes the GPU.

    Raises :class:`AcceleratorRejection` for unsupported accelerators, counts
    other than 1, or unknown names.
    """
    if accelerator is None:
        resolved_machine_type = resolve_compute_machine_type(
            accelerator=None,
            machine_type=machine_type,
        )
        return get_vm_rate_per_second(resolved_machine_type, spot=spot)

    resolved_machine_type = resolve_compute_machine_type(
        accelerator=accelerator,
        machine_type=machine_type,
    )
    spec, reason = validate_compute_request(
        accelerator,
        resolved_machine_type,
        count,
    )
    if reason is not None:
        raise AcceleratorRejection(reason)
    assert spec is not None

    if spec.bundled:
        hourly = GPU_BUNDLED_MACHINE_RATES[spec.machine_type]
        rate = hourly[1] if spot else hourly[0]
        return rate / 3600

    base_hourly = GPU_BASE_MACHINE_RATES[resolved_machine_type]
    surcharge_hourly = GPU_ACCELERATOR_SURCHARGE_RATES[spec.gcp_type]
    base_rate = base_hourly[1] if spot else base_hourly[0]
    surcharge_rate = surcharge_hourly[1] if spot else surcharge_hourly[0]
    return (base_rate + surcharge_rate) / 3600


# ---------------------------------------------------------------------------
# SQL-side cost aggregation
#
# These builders return SQLAlchemy expressions that compute a task's total VM
# cost entirely in the database, so cost dashboards can SUM/GROUP BY without
# hydrating thousands of JSONB-heavy Task rows into Python (the dominant cost
# of the old row-loading loops).  The per-row value mirrors
# ``models.get_task_total_vm_cost_usd`` exactly:
#
#     COALESCE(vm_runtime_cost_banked_db, 0) + current_attempt_cost
#
# where ``current_attempt_cost = max(0, runtime_seconds) * rate`` and ``rate``
# is the per-second rate for the task's (machine_type, spot) pair.
#
# Accuracy caveat: the per-second rate CASE covers every explicitly priced
# machine type (the e2 CPU shapes and the Cloud Run sentinels).  A task on an
# unpriced/unknown machine type falls back to the default e2-standard-2 rate,
# matching ``_resolve_rate_entry``'s fallback.  GPU shapes are not priced here
# (they are not surfaced by these cost widgets).
# ---------------------------------------------------------------------------


def _vm_rate_per_second_sql_case(task_cls: Any):
    """Return a SQLAlchemy ``case()`` for the per-second VM rate of each row.

    Mirrors :func:`get_vm_rate_per_second`: a NULL ``vm_machine_type`` resolves
    to the default (``MACHINE_TYPE`` env default, which is
    ``DEFAULT_MACHINE_TYPE``), Cloud Run sentinels use their fixed shape rate
    (spot has no meaning there), and every other machine type uses its
    on-demand vs. spot rate.
    """
    machine_type = func.coalesce(task_cls.vm_machine_type, DEFAULT_MACHINE_TYPE)
    whens: list = []
    # Cloud Run sentinels: fixed per-second rate, spot-agnostic.
    for cr_type in _CLOUD_RUN_SHAPES:
        whens.append((machine_type == cr_type, get_cloud_run_rate_for_machine_type(cr_type)))
    # CPU (e2) shapes: distinct on-demand vs. spot per-second rate.
    for mtype, (on_demand_hourly, spot_hourly) in VM_COST_RATES.items():
        whens.append(
            (and_(machine_type == mtype, task_cls.vm_spot.is_(True)), spot_hourly / 3600.0)
        )
        whens.append((machine_type == mtype, on_demand_hourly / 3600.0))
    default_on_demand, default_spot = VM_COST_RATES[DEFAULT_MACHINE_TYPE]
    return case(
        *whens,
        else_=case(
            (task_cls.vm_spot.is_(True), default_spot / 3600.0),
            else_=default_on_demand / 3600.0,
        ),
    )


def _runtime_seconds_sql(task_cls: Any, dialect_name: str):
    """Return VM runtime seconds (``vm_stopped_at - vm_provisioned_at``).

    NULL when either endpoint is NULL (no current attempt), matching
    ``get_task_current_attempt_vm_cost_usd`` which returns 0 in that case.
    """
    if dialect_name == "postgresql":
        return func.extract(
            "epoch", task_cls.vm_stopped_at - task_cls.vm_provisioned_at
        )
    # SQLite (test harness fallback): julianday() days -> seconds.
    return (
        func.julianday(task_cls.vm_stopped_at)
        - func.julianday(task_cls.vm_provisioned_at)
    ) * 86400.0


def vm_cost_sql_expr(dialect_name: str):
    """SQLAlchemy expression for a Task's total VM cost (banked + current attempt).

    Numerically equal to ``models.get_task_total_vm_cost_usd`` per row. Rows with
    no cost evidence evaluate to 0 here; callers that must exclude them entirely
    (rather than count them at $0) should also apply :func:`vm_cost_evidence_filter`.
    """
    from app.models import Task  # local import avoids models<->vm_cost_rates cycle

    runtime_seconds = _runtime_seconds_sql(Task, dialect_name)
    rate = _vm_rate_per_second_sql_case(Task)
    # Only positive runtime contributes (mirrors the max(0, …) / <=0 guard).
    current_attempt_cost = case((runtime_seconds > 0, runtime_seconds * rate), else_=0.0)
    return func.coalesce(Task.vm_runtime_cost_banked_db, 0.0) + current_attempt_cost


def vm_cost_evidence_filter():
    """SQL predicate for "this task has VM cost evidence".

    Mirrors ``models.get_task_total_vm_cost_usd_or_none``'s ``has_evidence`` gate
    (a positive banked amount OR a provisioning window), i.e. the rows for which
    that helper returns a value rather than ``None``.
    """
    from app.models import Task  # local import avoids models<->vm_cost_rates cycle

    return or_(
        and_(
            Task.vm_runtime_cost_banked_db.isnot(None),
            Task.vm_runtime_cost_banked_db > 0,
        ),
        Task.vm_provisioned_at.isnot(None),
    )
