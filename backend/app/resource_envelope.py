"""Helpers for per-project resource envelope resolution and spend aggregation.

The resource envelope is stored inside ``project.build_config["resource_envelope"]``
as a JSONB sub-object.  Resolvers guard against legacy/hand-edited values (missing
keys, wrong types) using the same isinstance pattern as
``autonomous_research.project_parallelism_cap``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select

if TYPE_CHECKING:
    from app.models import Project


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
    raw = _get_envelope(project).get("require_local_runner")
    return raw is True


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
    from app.models import Task, get_task_total_vm_cost_usd

    now = _utcnow()
    since = now - timedelta(days=window_days)

    compute_spend = await get_recent_terminal_compute_spend_usd(
        session,
        project_id=project_id,
        since=since,
        until=None,
    )

    result = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.updated_at >= since,
        )
    )
    tasks = result.scalars().all()
    task_vm_spend = sum(get_task_total_vm_cost_usd(t) for t in tasks)

    task_llm_anthropic = 0.0
    task_llm_non_anthropic = 0.0
    for t in tasks:
        a, na, _ = _task_llm_cost_split(t)
        task_llm_anthropic += a
        task_llm_non_anthropic += na
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
    return {
        "total": total,
        "compute": compute_f,
        "agent_vm": agent_vm_f,
        "idle": idle_f,
        "llm": llm_f,
        "ceiling_gate_total": total + non_anthropic_buffer,
    }
