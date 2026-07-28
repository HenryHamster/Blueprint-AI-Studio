"""Helpers for per-project resource envelope resolution and spend aggregation.

The resource envelope is stored inside ``project.build_config["resource_envelope"]``
as a JSONB sub-object.  Resolvers guard against legacy/hand-edited values (missing
keys, wrong types) using the same isinstance pattern as
``autonomous_research.project_parallelism_cap``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select

if TYPE_CHECKING:
    from app.models import Project


ExecutionPlacementPolicy = Literal[
    "local_only", "local_preferred", "managed_allowed"
]
"""How a project's executable work may be placed.

``local_only`` never permits managed/cloud execution, ``local_preferred``
uses local capacity first but permits managed fallback, and
``managed_allowed`` permits either placement.  The explicit value is new; a
legacy ``require_local_runner`` value remains compute-experiment-only.
"""

EXECUTION_PLACEMENT_POLICIES = frozenset(
    {"local_only", "local_preferred", "managed_allowed"}
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _get_envelope(project: Project) -> dict:
    """Return the raw resource_envelope dict from build_config, or {}."""
    bc = getattr(project, "build_config", None) or {}
    raw = bc.get("resource_envelope")
    if isinstance(raw, dict):
        return raw
    return {}


def resolve_project_vm_cap(project: Project) -> int | None:
    """Return the per-project concurrent-VM cap, or None if unlimited.

    Reads ``project.build_config["resource_envelope"]["max_concurrent_vms"]``.
    Returns None when absent, null, or not a valid positive integer — callers
    should then fall back to the global runtime setting.
    """
    raw = _get_envelope(project).get("max_concurrent_vms")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
        return raw
    return None


def resolve_project_spend_ceiling(project: Project) -> float | None:
    """Return the per-project spend ceiling in USD, or None if unlimited.

    Reads ``project.build_config["resource_envelope"]["spend_ceiling_usd"]``.
    Returns None when absent, null, or not a valid positive number — callers
    should then treat the project as uncapped.
    """
    raw = _get_envelope(project).get("spend_ceiling_usd")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return float(raw)
    return None


def resolve_project_spend_window_days(project: Project) -> int:
    """Return the spend aggregation window in days.

    Reads ``project.build_config["resource_envelope"]["spend_window_days"]``.
    Returns the schema default (30) when absent, null, or not a valid positive
    integer so hand-edited legacy config cannot crash scheduler hot paths.
    """
    raw = _get_envelope(project).get("spend_window_days")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
        return raw
    return 30


def resolve_project_hardware_policy(
    project: Project,
) -> tuple[list[str] | None, list[str] | None]:
    """Return (allowed_machine_types, allowed_accelerator_types) from the envelope.

    Returns (None, None) when absent or not lists — callers treat None as unlimited.
    """
    env = _get_envelope(project)
    machine_types = env.get("allowed_machine_types")
    accelerator_types = env.get("allowed_accelerator_types")
    allowed_machines = machine_types if isinstance(machine_types, list) else None
    allowed_accels = accelerator_types if isinstance(accelerator_types, list) else None
    return allowed_machines, allowed_accels


def resolve_project_require_local_runner(project: Project) -> bool:
    """Return True when this project's compute_experiment tasks must run on a
    registered self-hosted local runner (never billable cloud fallback).

    Reads ``project.build_config["resource_envelope"]["require_local_runner"]``.
    Returns False when absent, null, or not a bool — i.e. cloud fallback is
    permitted by default (legacy/hand-edited config cannot accidentally enable
    a hold-the-project policy).
    """
    env = _get_envelope(project)
    # The replacement policy applies to all supported executable work.  Never
    # combine that broader policy with the legacy compute-experiment-only flag,
    # even when a hand-edited envelope contains both fields.
    if env.get("execution_placement_policy") in EXECUTION_PLACEMENT_POLICIES:
        return False
    raw = env.get("require_local_runner")
    return raw is True


def resolve_project_execution_placement_policy(
    project: Project,
) -> ExecutionPlacementPolicy:
    """Return the project's explicit execution-placement policy.

    Invalid or absent hand-edited values deliberately resolve to
    ``managed_allowed`` so legacy projects retain their existing behaviour.
    In particular, legacy ``require_local_runner=true`` is intentionally *not*
    translated to ``local_only``: it only ever applied to compute experiments.
    Callers that enforce the legacy compute-experiment contract must continue
    to use :func:`resolve_project_require_local_runner` as well.
    """
    raw = _get_envelope(project).get("execution_placement_policy")
    if raw in EXECUTION_PLACEMENT_POLICIES:
        return raw
    return "managed_allowed"


def project_has_explicit_execution_placement_policy(project: Project) -> bool:
    """Whether a valid broad placement policy is stored on the project."""
    return (
        _get_envelope(project).get("execution_placement_policy")
        in EXECUTION_PLACEMENT_POLICIES
    )


def resolve_project_pause_on_ceiling_exceeded(project: Project) -> bool:
    """Return True when the project should auto-pause on spend ceiling breach.

    Reads ``project.build_config["resource_envelope"]["pause_on_ceiling_exceeded"]``.
    Returns False when absent, null, or not a bool — soft-defer (no pause) is the
    default so legacy projects are unaffected.
    """
    raw = _get_envelope(project).get("pause_on_ceiling_exceeded")
    return raw is True


def resolve_project_spend_warning_fraction(project: Project) -> float:
    """Return the warning fraction of the spend ceiling, defaulting to 0.8.

    Reads ``project.build_config["resource_envelope"]["spend_warning_fraction"]``.
    Returns 0.8 when absent, null, or not a valid float in (0, 1] so hand-edited
    legacy config cannot crash the spawn loop.
    """
    raw = _get_envelope(project).get("spend_warning_fraction")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and 0 < raw <= 1:
        return float(raw)
    return 0.8


async def get_project_spend_window_tasks(
    session,
    *,
    project_id: uuid.UUID,
    window_days: int = 30,
):
    """Return the project's tasks whose ``updated_at`` falls inside the rolling
    spend window ``[now - window_days, now)``.

    This is the exact task set the LLM/agent-VM spend components aggregate over,
    exposed so trustworthiness/diagnostic callers reconcile against the SAME
    window the ceiling gate uses.
    """
    from app.models import Task

    now = _utcnow()
    since = now - timedelta(days=window_days)
    result = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.updated_at >= since,
        )
    )
    return result.scalars().all()


async def get_project_recent_spend_usd(
    session,
    *,
    project_id: uuid.UUID,
    window_days: int = 30,
) -> dict[str, float]:
    """Return project spend (USD) over a rolling window, broken down by source.

    Combines four spend sources:

    1. **Settled compute-job spend** — ``final_cost_usd`` over terminal
       ``ComputeJob`` rows whose ``completed_at`` falls within
       ``[now - window_days, now)`` for the project.

    2. **Agent-VM task cost** — sum of ``get_task_total_vm_cost_usd`` over
       all tasks belonging to the project whose ``updated_at`` falls within
       the same window.  ``updated_at`` is used as the inclusion criterion
       because it reflects the last active/settled moment for both in-flight
       and terminal tasks; tasks that were provisioned before but still running
       (or finished) during the window are captured.

    3. **LLM API cost** — sum of ``_task_llm_cost`` over the same tasks,
       pricing each attempt's token usage via the ``LLM_TOKEN_PRICING`` table.

    4. **Warm-pool idle cost** — attributed idle cost for this project's pool
       VMs over the same rolling window, via
       ``efficiency_snapshot._compute_windowed_idle_cost``.  Includes both
       settled ``PoolIdleSpan`` rows (``settled_at >= since``) and the
       in-progress idle portion of any currently-IDLE pool VMs.

    All four components use the same ``[now - window_days, now)`` window so
    the total is consistent across dispatch-gate, spawn-gate, and operator
    display callers.

    Returns a dict with keys ``total``, ``compute``, ``agent_vm``, ``llm``,
    ``idle``.
    Callers that only need the ceiling-gate check should use ``result["total"]``.
    """
    from app.compute_jobs import get_recent_terminal_compute_spend_usd
    from app.efficiency_snapshot import _compute_windowed_idle_cost
    from app.llm_cost_attribution import (
        NON_ANTHROPIC_LLM_CONSERVATISM_MULTIPLIER,
        _task_llm_cost_split,
    )
    from app.models import get_task_total_vm_cost_usd

    now = _utcnow()
    since = now - timedelta(days=window_days)

    compute_spend = await get_recent_terminal_compute_spend_usd(
        session,
        project_id=project_id,
        since=since,
        until=None,
    )

    tasks = await get_project_spend_window_tasks(
        session, project_id=project_id, window_days=window_days
    )
    task_vm_spend = sum(get_task_total_vm_cost_usd(t) for t in tasks)

    task_llm_anthropic = 0.0
    task_llm_non_anthropic = 0.0
    any_unpriced = False
    for t in tasks:
        a, na, unpriced = _task_llm_cost_split(t)
        task_llm_anthropic += a
        task_llm_non_anthropic += na
        any_unpriced = any_unpriced or unpriced
    task_llm_spend = task_llm_anthropic + task_llm_non_anthropic

    idle_cost_result = await _compute_windowed_idle_cost(session, project_id, since)
    idle_spend = idle_cost_result["total"]

    compute_f = float(compute_spend)
    agent_vm_f = float(task_vm_spend)
    idle_f = float(idle_spend)
    llm_f = float(task_llm_spend)
    total = compute_f + agent_vm_f + idle_f + llm_f
    # Non-Anthropic LLM cost cannot be independently verified (no per-call usage
    # probe), so the ceiling gate uses a conservative total that inflates the
    # non-Anthropic portion. Raw total and llm keys are unchanged for reporting.
    non_anthropic_buffer = (NON_ANTHROPIC_LLM_CONSERVATISM_MULTIPLIER - 1.0) * float(
        task_llm_non_anthropic
    )
    # Additive-only confidence fields (do NOT alter enforcement math): the split
    # of LLM cost into Anthropic-verifiable vs self-reported non-Anthropic, and a
    # coarse confidence label derived purely from that composition (no DB probe
    # required — the independent probe / coverage_fraction is surfaced separately
    # by ``assess_ceiling_gate_trustworthiness``).
    non_anthropic_share = (
        float(task_llm_non_anthropic) / llm_f if llm_f > 0 else 0.0
    )
    return {
        "total": total,
        "compute": compute_f,
        "agent_vm": agent_vm_f,
        "idle": idle_f,
        "llm": llm_f,
        "ceiling_gate_total": total + non_anthropic_buffer,
        "llm_anthropic": float(task_llm_anthropic),
        "llm_non_anthropic": float(task_llm_non_anthropic),
        "llm_unpriced": bool(any_unpriced),
        "non_anthropic_share": non_anthropic_share,
        "confidence": spend_confidence_label(
            non_anthropic_llm=float(task_llm_non_anthropic),
            any_unpriced=any_unpriced,
        ),
    }


def spend_confidence_label(
    *,
    non_anthropic_llm: float,
    any_unpriced: bool,
) -> str:
    """Return a coarse confidence label for a project's weighted spend figure.

    - ``"low"`` when any LLM usage is UNPRICED/unverifiable (a model/provider
      with no pricing row): its dollar contribution is silently zero, so the
      recorded total may under-count real spend in a way the buffer cannot cover.
    - ``"estimated"`` when there is a priced but self-reported non-Anthropic
      share (no per-call probe exists for these providers).
    - ``"verified"`` otherwise — all priced LLM cost is Anthropic, the only
      component with an independent per-call usage probe.

    The label reflects only the COMPOSITION of the spend; whether the Anthropic
    portion actually reconciles within tolerance is reported separately by
    :func:`assess_ceiling_gate_trustworthiness`.
    """
    if any_unpriced:
        return "low"
    if non_anthropic_llm > 0:
        return "estimated"
    return "verified"


def assess_ceiling_gate_trustworthiness(tasks) -> dict:
    """Verify the weighted ceiling-gate spend figure against real recorded spend.

    Given a list of real :class:`~app.models.Task` rows this returns a structured
    verdict proving the confidence-weighted figure the dispatch gate consumes is
    SAFE — an inaccurate cap is worse than none, so the gate must never under-count
    real recorded spend. Pure (no DB); reusable by an operator/diagnostic against
    real historical tasks.

    Checks:
      (a) ``ceiling_gate_total >= total`` always holds — the gate is a CONSERVATIVE
          upper bound and can never under-count real recorded LLM spend.
      (b) the non-Anthropic buffer equals exactly
          ``(multiplier - 1.0) * priced-non-Anthropic cost``.
      (c) for the Anthropic portion — the one component with an independent probe —
          it runs :func:`reconcile_provider_reported_usage` and reports whether it
          is within tolerance.
      (d) it reports the self-reported (non-Anthropic) share of total LLM cost and
          the reconciliation ``coverage_fraction``.
    """
    from app.cost_telemetry_reconciliation import reconcile_provider_reported_usage
    from app.llm_cost_attribution import (
        NON_ANTHROPIC_LLM_CONSERVATISM_MULTIPLIER,
        _task_llm_cost_split,
    )

    task_list = list(tasks)
    anthropic_llm = 0.0
    non_anthropic_llm = 0.0
    any_unpriced = False
    for t in task_list:
        a, na, unpriced = _task_llm_cost_split(t)
        anthropic_llm += a
        non_anthropic_llm += na
        any_unpriced = any_unpriced or unpriced

    total_llm = anthropic_llm + non_anthropic_llm
    buffer = (NON_ANTHROPIC_LLM_CONSERVATISM_MULTIPLIER - 1.0) * non_anthropic_llm
    ceiling_gate_total = total_llm + buffer

    # (a) The buffer is non-negative, so this must hold by construction; assert it
    # numerically anyway so a future change to the buffer math is caught here.
    conservative_upper_bound = ceiling_gate_total >= total_llm - 1e-9

    # (b) Buffer exactness.
    expected_buffer = (
        NON_ANTHROPIC_LLM_CONSERVATISM_MULTIPLIER - 1.0
    ) * non_anthropic_llm
    buffer_exact = abs(buffer - expected_buffer) <= 1e-9

    # (c) Independent Anthropic probe reconciliation.
    reconciliation = reconcile_provider_reported_usage(task_list)
    provider_reported_within_tolerance = bool(
        reconciliation.provider_reported_within_tolerance
    )
    coverage_fraction = reconciliation.coverage_fraction

    # (d) Self-reported (non-Anthropic) share of total LLM cost.
    non_anthropic_share = (
        non_anthropic_llm / total_llm if total_llm > 0 else 0.0
    )

    trustworthy = (
        conservative_upper_bound
        and buffer_exact
        and provider_reported_within_tolerance
    )
    if not conservative_upper_bound:
        reason = "ceiling_gate_total under-counts recorded spend"
    elif not buffer_exact:
        reason = "non_anthropic_buffer does not match the conservatism multiplier"
    elif not provider_reported_within_tolerance:
        reason = "anthropic provider-reported usage failed reconciliation"
    else:
        reason = "weighted figure is a conservative upper bound of recorded spend"

    return {
        "trustworthy": trustworthy,
        "reason": reason,
        "total": total_llm,
        "ceiling_gate_total": ceiling_gate_total,
        "anthropic_llm_usd": anthropic_llm,
        "non_anthropic_llm_usd": non_anthropic_llm,
        "non_anthropic_buffer_usd": buffer,
        "non_anthropic_share": non_anthropic_share,
        "any_unpriced": any_unpriced,
        "conservative_upper_bound": conservative_upper_bound,
        "buffer_exact": buffer_exact,
        "provider_reported_within_tolerance": provider_reported_within_tolerance,
        "coverage_fraction": coverage_fraction,
        "confidence": spend_confidence_label(
            non_anthropic_llm=non_anthropic_llm,
            any_unpriced=any_unpriced,
        ),
        "reconciliation": reconciliation.asdict(),
    }
