"""Local self-hosted runner resolution for strategic spawning.

Cohesive cluster extracted verbatim from ``strategic_spawning.py`` to keep that
module under its size cap. These helpers surface live local-runner availability
for the strategist's context and resolve advisory runner affinity / hard local
intent for fresh continuous-local Strategic mode.

Every name defined here is re-exported from ``app.strategic_spawning`` so all
existing ``from app.strategic_spawning import ...`` call sites and tests keep
resolving unchanged.
"""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.experiment_authoring_context import build_runner_devices
from app.models import LocalRunner, Project
from app.time_utils import _utcnow
from app.workbench_state import (
    STRATEGIC_LOCAL_CONTINUATION_KEY,
    STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY,
    strategic_local_continuation_status_from_vision,
)


# Mirrors ``orchestrator._RUNNER_LIVENESS_SECONDS`` (300): a registered runner is
# considered live only if it heartbeated within this window. Replicated here
# rather than imported to avoid an ``orchestrator -> autonomous_research ->
# strategic_spawning`` import cycle (strategic_spawning is pulled in lazily by
# the orchestrator's autonomous_research call chain; importing orchestrator at
# this module's top would invert that order). Keep in sync with orchestrator.
_RUNNER_LIVENESS_SECONDS = 300


async def _load_local_runner_status(
    session: AsyncSession, project_id: uuid.UUID
) -> dict[str, Any]:
    """Live self-hosted local-runner availability for the strategist's context.

    Surfaces whether the operator's self-hosted GPU hardware is up so the
    strategist can avoid authoring GPU ``compute_experiment`` tasks that would
    otherwise silently fall back to billable cloud GPU while the hardware is
    offline. Informational only — there is NO dispatch effect or hard block.

    Returns ``{"ever_registered": bool, "active_total": int, "active_gpu": int}``:

    * ``ever_registered`` — the project has >=1 non-deleted ``LocalRunner`` row
      (operator set up local hardware at some point).
    * ``active_total`` — runners with ``status=='active'`` that heartbeated within
      the liveness window (mirrors the orchestrator dispatch-cache predicate).
    * ``active_gpu`` — subset of ``active_total`` whose ``capabilities.has_gpu`` is
      truthy.

    ADDITIVE: when at least one runner is live, a ``devices`` key carries a
    bounded per-runner hardware list (name, gpu_count, gpu_models, per-GPU
    VRAM when advertised, container capability, current load) built by
    :func:`app.experiment_authoring_context.build_runner_devices` — so
    authoring agents can reason about VRAM fit and device counts. The key is
    OMITTED when no runner is live, keeping the exact 3-key shape above for
    its existing consumers and tests (the shape is load-bearing:
    ``strategic_spawning`` merges ``require_local_runner`` at the call site
    and agent-side renderers read the three counters by name).

    One query (non-deleted runners for the project — a small set); the liveness
    + GPU filtering is done in Python to stay dialect-agnostic over the JSONB
    ``capabilities`` column.
    """
    rows = (
        await session.execute(
            select(LocalRunner).where(
                LocalRunner.project_id == project_id,
                LocalRunner.status != "deleted",
            )
        )
    ).scalars().all()
    cutoff = _utcnow() - timedelta(seconds=_RUNNER_LIVENESS_SECONDS)
    active_total = 0
    active_gpu = 0
    live_runners: list[LocalRunner] = []
    for runner in rows:
        last_seen = runner.last_seen_at
        if runner.status == "active" and last_seen is not None and last_seen > cutoff:
            active_total += 1
            live_runners.append(runner)
            if (runner.capabilities or {}).get("has_gpu"):
                active_gpu += 1
    status: dict[str, Any] = {
        "ever_registered": len(rows) > 0,
        "active_total": active_total,
        "active_gpu": active_gpu,
    }
    if live_runners:
        devices = build_runner_devices(live_runners)
        if devices:
            status["devices"] = devices
    return status


def _agent_visible_vision(vision: dict[str, Any] | None) -> dict[str, Any]:
    """Return project vision without backend-only operator session metadata."""

    visible = {
        key: value
        for key, value in (vision or {}).items()
        if not key.startswith("_")
    }
    visible.pop(STRATEGIC_LOCAL_CONTINUATION_KEY, None)
    return visible


def _local_runner_handles_task_type(runner: LocalRunner, task_type: str) -> bool:
    task_types = (runner.capabilities or {}).get("task_types", "all")
    if task_types == "all":
        return True
    if isinstance(task_types, (list, tuple, set)):
        return task_type in task_types
    return False


def _local_runner_is_live(runner: LocalRunner) -> bool:
    cutoff = _utcnow() - timedelta(seconds=_RUNNER_LIVENESS_SECONDS)
    return (
        runner.status == "active"
        and runner.last_seen_at is not None
        and runner.last_seen_at > cutoff
    )


def _local_runner_agent_available(runner: LocalRunner) -> bool:
    return (runner.capabilities or {}).get("agent_available") is not False


async def _resolve_local_continuation_runner_affinity(
    session: AsyncSession,
    project_id: Any,
    task_type: str,
) -> str | None:
    """Return advisory runner affinity for fresh continuous local Strategic mode."""

    current_vision = (
        await session.execute(select(Project.vision).where(Project.id == project_id))
    ).scalar_one_or_none()
    status = strategic_local_continuation_status_from_vision(current_vision or {})
    if not status.get("enabled") or not status.get("fresh"):
        return None

    rows = (
        await session.execute(
            select(LocalRunner)
            .where(
                LocalRunner.project_id == project_id,
                LocalRunner.status != "deleted",
            )
            .order_by(
                LocalRunner.last_seen_at.desc(),
                LocalRunner.created_at.desc(),
            )
        )
    ).scalars().all()
    live_capable = [
        runner
        for runner in rows
        if _local_runner_is_live(runner)
        and _local_runner_handles_task_type(runner, task_type)
        and _local_runner_agent_available(runner)
    ]
    if not live_capable:
        return None

    preferred_runner_id = status.get("preferred_runner_id")
    if isinstance(preferred_runner_id, str) and preferred_runner_id.strip():
        preferred_runner_id = preferred_runner_id.strip()
        for runner in live_capable:
            if str(runner.id) == preferred_runner_id:
                return preferred_runner_id

    return str(live_capable[0].id)


async def _resolve_local_continuation_runner_intent(
    session: AsyncSession,
    project_id: Any,
    task_type: str,
) -> dict[str, Any]:
    """Return dispatch context for fresh continuous local Strategic mode.

    A fresh continuation marker is a hard local intent, even when no runner is
    live at spawn time. The runner id remains advisory within the local fleet;
    the explicit required flag is what prevents cloud fallback later.

    Freshness ("fresh") is resolved by
    ``strategic_local_continuation_status_from_vision``, which reads the live
    ``strategic_continuation_ttl_seconds`` runtime setting (default 1500s / 25
    min) rather than the retired hardcoded 12h TTL — so a marker that stopped
    renewing (no operator interaction / no applied strategic op) goes stale
    within the short lease window and this hard-local intent is NOT applied,
    letting the next spawn dispatch to cloud.
    """

    current_vision = (
        await session.execute(select(Project.vision).where(Project.id == project_id))
    ).scalar_one_or_none()
    status = strategic_local_continuation_status_from_vision(current_vision or {})
    if not status.get("enabled") or not status.get("fresh"):
        return {}

    context: dict[str, Any] = {
        STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
        STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY: "workbench_continuous",
    }
    preferred_runner_target_id = await _resolve_local_continuation_runner_affinity(
        session,
        project_id,
        task_type,
    )
    if preferred_runner_target_id:
        context["preferred_runner_target_id"] = preferred_runner_target_id
    return context
