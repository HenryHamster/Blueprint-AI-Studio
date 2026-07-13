"""Local-runner + compute-experiment dispatch cluster, extracted from
``orchestrator_worker``.

This module owns the pre-provision dispatch stage that pins QUEUED tasks to
registered local runners and dispatches compute-experiment tasks to compute
jobs: the runner-liveness cache, capacity probes, strategic-local hold/timeout
handling, direct compute-experiment dispatch, stale-runner deactivation, and
the all-accounts-blocked stall alert. It was lifted verbatim out of the
``orchestrator_worker`` inline region (behaviour-preserving) to shrink the
orchestrator monolith, mirroring the session-parameterized shape of the sibling
``orchestrator_ci_monitor`` / ``dep_lifecycle`` / ``orchestrator_liveness``
extractions: every helper takes the caller's ``session`` (or a
session-independent snapshot) and preserves each log prefix and commit/error
path.

``_clear_dispatch_prep_failures`` stays in ``app.orchestrator`` and is imported
lazily inside ``_try_runner_dispatch`` to avoid a load-time import cycle (the
orchestrator module re-exports the symbols below). All extracted symbols are
re-exported from ``app.orchestrator`` so ``from app.orchestrator import ...``
keeps working for existing callers and tests.
"""

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    ComputeJob,
    ComputeJobStatus,
    LocalRunner,
    Project,
    Task,
    TaskStatus,
)
from app.resource_envelope import resolve_project_require_local_runner
from app.runner_health_monitors import (  # noqa: F401
    _RUNNER_DEGRADED_FAILURE_THRESHOLD,
    _RUNNER_LIVENESS_SECONDS,
    _RUNNER_LOW_DISK_THRESHOLD_BYTES_DEFAULT,
    _RUNNER_LOW_MEMORY_THRESHOLD_BYTES_DEFAULT,
    _check_degraded_runners,
    _check_fleet_no_healthy_runner,
    _check_idle_gpu_runners,
    _check_low_disk_runners,
    _check_low_memory_runners,
    _sweep_runner_deactivated_escalations,
)
from app.shared_state import runtime_config as _runtime_config
from app.stall_alert_diagnostics import (
    _STALL_TITLE_PREFIX,
    _format_stall_alert,
)
from app.task_capabilities import STRATEGIC_TASK_TYPES
from app.time_utils import _utcnow
from app.workbench_events import (
    SURFACE_RUNTIME,
    SURFACE_STRATEGIC_SESSION,
    emit_workbench_refetch,
)
from app.workbench_state import (
    STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY,
)

logger = logging.getLogger("blueprint")


# task.context key holding an operator's HARD runner pin for a compute_experiment.
# Kept deliberately SEPARATE from the soft ``preferred_runner_target_id`` affinity
# hint: a forced target NEVER silently falls back to a different runner or to
# cloud — when it is not eligible this cycle the task is HELD QUEUED. The soft
# preferred hint keeps its fallback behaviour untouched.
FORCED_RUNNER_TARGET_CONTEXT_KEY = "forced_runner_target_id"

# task.context marker stamped (ISO-8601) the first cycle a forced-pin
# compute_experiment is held waiting for its target runner to become eligible —
# the honest, operator-legible "waiting for dgx-1" signal, mirroring the
# ``awaiting_local_runner_since`` / ``awaiting_container_runner_since`` holds.
AWAITING_FORCED_RUNNER_CONTEXT_KEY = "awaiting_forced_runner_since"


# A compute_experiment held for a require-local project is failed if no runner
# claims it within this window — prevents a permanently-wedged project when the
# operator's hardware stays offline. Generous default; operators can bring a
# runner up well within it.
_LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS = 6 * 3600

# Early-warning threshold: emit a proactive WARNING alert when a require-local
# compute_experiment hold has waited past this window but before the 6h hard
# timeout — so the operator learns the hardware is offline in minutes, not hours.
_LOCAL_RUNNER_HOLD_ALERT_SECONDS = 15 * 60  # 15 minutes


class RunnerDispatchSnapshot(NamedTuple):
    """Session-independent view of a live local runner used during dispatch."""

    id: uuid.UUID
    project_id: uuid.UUID
    capabilities: dict
    consecutive_job_failures: int = 0
    hostname: str | None = None


def _runner_handles_task_type(
    runner: LocalRunner | RunnerDispatchSnapshot, task_type: str
) -> bool:
    """Whether a runner covers this task type, honoring BOTH policy and capability.

    Two independent gates, intersected:

    * **Operator policy (ceiling)** — ``capabilities['task_types']`` is
      operator-configured (``"all"`` or an allowlist). It is the maximum set the
      operator permits this runner to serve.
    * **Runner-declared capability (floor)** — ``capabilities['supported_agent_task_types']``
      is advertised by the runner itself over the heartbeat: a docker-only box
      (docker+image, no bwrap) declares the read-only agent slice it can actually
      run. When present (not None), an AGENT task type must also appear in it, so
      the orchestrator never pins a write/heavy type this box would decline after
      dispatch. Absent/None → no extra restriction (backward compatible with
      runners that never advertise it).

    ``compute_experiment`` is a COMPUTE-class task fulfilled by a different backend
    (the runner's ``decide_compute_backend``, not the agent path), so the
    agent-only floor never applies to it — a docker-only runner still handles
    compute jobs. This mirrors the ``task_type == "compute_experiment"`` carve-out
    the caller already applies to ``_runner_agent_available``.
    """
    caps = runner.capabilities or {}
    task_types = caps.get("task_types", "all")
    if task_types == "all":
        policy_ok = True
    elif isinstance(task_types, (list, tuple, set)):
        policy_ok = task_type in task_types
    else:
        # Malformed capability shape — be conservative and treat as no-cover.
        policy_ok = False
    if not policy_ok:
        return False

    # Runner-declared agent-capability floor. Only constrains AGENT task types;
    # compute_experiment is exempt (handled by the compute backend, not the agent).
    supported_agent = caps.get("supported_agent_task_types")
    if (
        supported_agent is not None
        and task_type != "compute_experiment"
        and isinstance(supported_agent, (list, tuple, set))
    ):
        return task_type in supported_agent
    return True


# Reserved capability key holding the AUTHORITATIVE active agent-task count that
# _load_active_runners_cache computes from Task rows for the dispatch cycle. Kept
# distinct from the heartbeat-reported ``active_task_ids`` so it can never be
# overwritten by a runner heartbeat payload.
_ACTIVE_AGENT_TASK_COUNT_KEY = "_active_agent_task_count"


def _runner_active_task_count(runner: LocalRunner | RunnerDispatchSnapshot) -> int:
    """Current AGENT-task load for the runner.

    Authoritative source: the count of the runner's own Task rows in an active
    status (PROVISIONING/IN_PROGRESS pinned to ``runner:<id>``), computed ONCE per
    dispatch cycle and injected as ``capabilities['_active_agent_task_count']`` by
    :func:`_load_active_runners_cache`. The heartbeat-reported ``active_task_ids``
    list is only a HINT: it can go stale-HIGH (still listing tasks that already
    COMPLETED/FAILED), which inflates the load and falsely trips
    :func:`_runner_at_capacity` — holding a pinned head while the runner is actually
    idle. Falls back to the heartbeat list only when the authoritative count is
    absent (a bare ``LocalRunner`` ORM outside the dispatch-cache path, or an old
    snapshot), preserving backward-compatible behaviour.
    """
    caps = runner.capabilities or {}
    authoritative = caps.get(_ACTIVE_AGENT_TASK_COUNT_KEY)
    if isinstance(authoritative, int) and not isinstance(authoritative, bool):
        return max(0, authoritative)
    active = caps.get("active_task_ids") or []
    try:
        return len(active)
    except TypeError:
        return 0


def _runner_active_compute_job_count(
    runner: LocalRunner | RunnerDispatchSnapshot,
) -> int:
    """Best-effort compute-job load, from heartbeat-reported active job ids."""
    active = (runner.capabilities or {}).get("active_compute_job_ids") or []
    try:
        return len(active)
    except TypeError:
        return 0


def _runner_active_work_count(runner: LocalRunner | RunnerDispatchSnapshot) -> int:
    return _runner_active_task_count(runner) + _runner_active_compute_job_count(runner)


def _runner_agent_available(runner: LocalRunner | RunnerDispatchSnapshot) -> bool:
    """Old runners omit the field, which remains backward-compatible."""
    available = (runner.capabilities or {}).get("agent_available")
    return available is not False


def _coerce_max_concurrent(raw) -> int | None:
    """Coerce a raw capability value to a positive int, or None if invalid."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


_CONTAINER_BACKEND_KEYS = {
    "docker_usable",
    "podman_usable",
    "apptainer_ok",
    "enroot_ok",
}


def _runner_has_container_capability(runner: LocalRunner | RunnerDispatchSnapshot) -> bool:
    """Whether a runner advertises a container isolation backend.

    Reads the heartbeat-reported ``isolation_backends`` map and returns True
    when at least one container-capable backend (docker, podman, apptainer,
    enroot) is reported as available. Runners that never report
    ``isolation_backends`` (old runners) are treated as NOT container-capable:
    the orchestrator cannot safely route a container-image job to a runner
    whose isolation support is unknown.
    """
    caps = runner.capabilities or {}
    backends = caps.get("isolation_backends")
    if not isinstance(backends, dict):
        return False
    return any(backends.get(key) for key in _CONTAINER_BACKEND_KEYS)


def _runner_has_free_vram(
    runner: LocalRunner | RunnerDispatchSnapshot,
    required_mib: int,
) -> bool:
    """Whether any GPU on this runner has at least ``required_mib`` MiB free.

    Reads the heartbeat-reported ``gpu_free_vram_mib`` list (one entry per
    GPU, index-aligned with ``gpu_names``). When the runner has not reported
    this field (older runners, or a runner that does not expose nvidia-smi)
    the check is skipped and the runner is treated as eligible — we only
    gate dispatch when we have real data, not on absence of telemetry.
    """
    caps = runner.capabilities or {}
    free_mib_list = caps.get("gpu_free_vram_mib")
    if not isinstance(free_mib_list, list) or not free_mib_list:
        return True
    return any(mib >= required_mib for mib in free_mib_list)


def _runner_gpu_capable(runner: LocalRunner | RunnerDispatchSnapshot) -> bool:
    """Whether a runner is trustworthy GPU-capable for accelerator dispatch.

    The runner self-reports ``has_gpu`` (a ``StrictBool``) in its heartbeat, but a
    mis-set flag would SILENTLY DEFEAT the GPU capability gate — the runner would win
    GPU-accelerator jobs it cannot execute, and they die missing-GPU on the box
    instead of holding for real hardware. We therefore do NOT trust the bare bool
    over the runner's own GPU TELEMETRY: the same heartbeat carries
    ``gpu_count`` / ``gpu_names`` (nvidia-smi-derived facts persisted alongside
    ``has_gpu`` in capabilities JSONB). When ``has_gpu`` is claimed true but that
    telemetry positively contradicts it (a reported count of 0, or an empty names
    list), the claim is disbelieved: the runner is treated as CPU-only and simply not
    sent accelerator jobs (they hold for a genuinely GPU-capable runner, per the
    existing no-eligible-runner hold paths — never a crash).

    Absence of telemetry is NOT a contradiction. An older runner (or one that does
    not expose nvidia-smi) may set ``has_gpu`` without reporting a count/names; as
    with :func:`_runner_has_free_vram`, we gate only on real contradicting data, not
    on missing telemetry — so a legitimately-configured runner that just does not
    report the extra fields is never demoted (backward compatible). ``gpu_count`` is
    authoritative when present; ``gpu_names`` is the fallback signal when the count
    is absent.
    """
    caps = runner.capabilities or {}
    if not caps.get("has_gpu"):
        return False
    # has_gpu is claimed true — cross-check the runner's own GPU telemetry, trusting
    # the fact over the bare bool when the fact is present.
    gpu_count = caps.get("gpu_count")
    if isinstance(gpu_count, int) and not isinstance(gpu_count, bool):
        return gpu_count > 0
    gpu_names = caps.get("gpu_names")
    if isinstance(gpu_names, list):
        return len(gpu_names) > 0
    # No GPU telemetry reported at all — trust the self-report (backward compatible).
    return True


def _runner_max_concurrent_compute(
    runner: LocalRunner | RunnerDispatchSnapshot,
) -> int | None:
    """Max concurrent compute jobs for this runner, defaulting to max_concurrent.

    Reads ``capabilities['max_concurrent_compute']`` when set by the runner
    heartbeat or operator PATCH.  Falls back to ``max_concurrent`` so runners
    that do not yet advertise a separate compute limit keep the old combined
    behaviour (backward compatible).
    """
    caps = runner.capabilities or {}
    raw = caps.get("max_concurrent_compute")
    if raw is not None:
        result = _coerce_max_concurrent(raw)
        if result is not None:
            return result
    # Fall back to the combined max_concurrent.
    return _coerce_max_concurrent(caps.get("max_concurrent"))


def _runner_at_compute_capacity(
    runner: LocalRunner | RunnerDispatchSnapshot, pins_this_cycle: int = 0
) -> bool:
    """True when the runner's compute-job load has reached max_concurrent_compute."""
    max_compute = _runner_max_concurrent_compute(runner)
    if max_compute is None:
        return False
    return _runner_active_compute_job_count(runner) + pins_this_cycle >= max_compute


def _runner_at_capacity(
    runner: LocalRunner | RunnerDispatchSnapshot,
    pins_this_cycle: int = 0,
    task_type: str = "agent",
) -> bool:
    """True when the runner's EFFECTIVE load has reached its class capacity limit.

    For ``compute_experiment`` tasks, checks the COMPUTE-class capacity:
    ``active_compute_job_count + pins >= max_concurrent_compute`` (falling back to
    ``max_concurrent`` when ``max_concurrent_compute`` is absent — backward compat).

    For all other task types (agent), checks the AGENT-class capacity:
    ``active_task_count + pins >= max_concurrent``.

    The in-cycle pin count prevents over-pinning within a single dispatch pass
    when the heartbeat-reported active count still reflects the old state.
    """
    caps = runner.capabilities or {}
    if task_type == "compute_experiment":
        return _runner_at_compute_capacity(runner, pins_this_cycle)
    # Agent task: check active AGENT task count against max_concurrent.
    max_concurrent = _coerce_max_concurrent(caps.get("max_concurrent"))
    if max_concurrent is None:
        return False
    return _runner_active_task_count(runner) + pins_this_cycle >= max_concurrent


async def _load_active_runners_cache(
    session: AsyncSession,
) -> dict[uuid.UUID, list[RunnerDispatchSnapshot]]:
    """Load live registered runners once per orchestrator cycle, by project.

    A runner is live when status='active' AND it heartbeated within the
    liveness window. Returns ``{project_id: [RunnerDispatchSnapshot]}``; an
    empty dict when no runner is registered (the common case — all existing
    dispatch paths then run unchanged).

    The cache is deliberately scalar/value-based, not ORM-instance-based. The
    dispatch cycle performs commits and rollbacks before later candidates reach
    runner selection; those transaction boundaries can expire attached ORM
    instances, and reading ``LocalRunner.capabilities`` later would lazy-load
    under ``AsyncSession`` and raise MissingGreenlet.
    """
    cutoff = _utcnow() - timedelta(seconds=_RUNNER_LIVENESS_SECONDS)
    try:
        rows = (
            await session.execute(
                select(
                    LocalRunner.id,
                    LocalRunner.project_id,
                    LocalRunner.capabilities,
                    LocalRunner.consecutive_job_failures,
                    LocalRunner.hostname,
                ).where(
                    LocalRunner.status == "active",
                    LocalRunner.last_seen_at.is_not(None),
                    LocalRunner.last_seen_at > cutoff,
                )
            )
        ).all()
    except Exception:
        logger.exception("[RUNNERS] Failed to load active-runner cache")
        return {}
    runner_ids_for_load = [
        runner_id for runner_id, _project_id, _capabilities, _failures, _hostname in rows
    ]
    outstanding_compute_by_runner = await _load_runner_compute_job_assignments(
        session, runner_ids_for_load
    )
    # Authoritative per-runner agent-task load from Task rows (heartbeat-independent).
    active_task_count_by_runner = await _load_runner_active_task_counts(
        session, runner_ids_for_load
    )
    by_project: dict[uuid.UUID, list[RunnerDispatchSnapshot]] = {}
    for runner_id, project_id, capabilities, consecutive_job_failures, hostname in rows:
        caps = dict(capabilities or {})
        # Inject the authoritative active agent-task count so _runner_active_task_count
        # / _runner_at_capacity size AGENT capacity off committed Task rows rather than
        # the stale-prone heartbeat ``active_task_ids`` list (which can over-report and
        # falsely pin-block an idle runner).  Inject ONLY when the count query
        # succeeded: on success the loader returns an entry for EVERY runner id (0
        # for idle), so a missing id means the query FAILED — in which case we leave
        # the key absent and _runner_active_task_count falls back to the heartbeat
        # hint instead of reading a false 0 that would let a loaded runner over-pin.
        if runner_id in active_task_count_by_runner:
            caps[_ACTIVE_AGENT_TASK_COUNT_KEY] = active_task_count_by_runner[runner_id]
        heartbeat_jobs = caps.get("active_compute_job_ids") or []
        merged_job_ids: list[str] = []
        seen_job_ids: set[str] = set()
        for job_id in list(heartbeat_jobs) + outstanding_compute_by_runner.get(
            runner_id, []
        ):
            if not isinstance(job_id, str) or job_id in seen_job_ids:
                continue
            merged_job_ids.append(job_id)
            seen_job_ids.add(job_id)
        if merged_job_ids:
            caps["active_compute_job_ids"] = merged_job_ids
        snapshot = RunnerDispatchSnapshot(
            id=runner_id,
            project_id=project_id,
            capabilities=caps,
            consecutive_job_failures=consecutive_job_failures or 0,
            hostname=hostname,
        )
        by_project.setdefault(project_id, []).append(snapshot)
    return by_project


async def _load_runner_compute_job_assignments(
    session: AsyncSession,
    runner_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[str]]:
    """Return outstanding runner-subprocess compute jobs keyed by runner id.

    Heartbeat-reported compute ids lag direct ``compute_experiment`` launch by at
    least one runner poll/heartbeat. Counting persisted runner-subprocess jobs
    closes that window so a later orchestrator cycle cannot over-assign a runner
    before it has claimed the first job.
    """
    if not runner_ids:
        return {}
    from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME

    runner_id_strings = [str(runner_id) for runner_id in runner_ids]
    runner_target_expr = ComputeJob.polling_metadata["runner_target_id"].as_string()
    try:
        rows = (
            await session.execute(
                select(ComputeJob.id, runner_target_expr)
                .where(
                    ComputeJob.backend == RUNNER_SUBPROCESS_BACKEND_NAME,
                    ComputeJob.status.in_(
                        [
                            ComputeJobStatus.QUEUED,
                            ComputeJobStatus.STARTING,
                            ComputeJobStatus.RUNNING,
                            ComputeJobStatus.POLLING,
                        ]
                    ),
                    runner_target_expr.in_(runner_id_strings),
                )
                .order_by(ComputeJob.created_at.asc())
            )
        ).all()
    except Exception:
        logger.exception("[RUNNERS] Failed to load runner compute-job assignments")
        return {}

    by_runner: dict[uuid.UUID, list[str]] = {}
    runner_ids_by_string = {
        str(runner_id): runner_id
        for runner_id in runner_ids
    }
    for job_id, raw_runner_id in rows:
        runner_id = runner_ids_by_string.get(str(raw_runner_id))
        if runner_id is None:
            continue
        by_runner.setdefault(runner_id, []).append(str(job_id))
    return by_runner


async def _load_runner_active_task_counts(
    session: AsyncSession,
    runner_ids: list[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """Return the AUTHORITATIVE active agent-task count per runner id.

    Counts committed Task rows pinned to each runner (``vm_instance_id ==
    'runner:<id>'``) that are still in an active status (PROVISIONING or
    IN_PROGRESS). This is the source of truth for a runner's agent load: unlike the
    heartbeat-reported ``active_task_ids`` list — which can go stale-HIGH by still
    listing tasks that already COMPLETED/FAILED and thus falsely trip
    ``_runner_at_capacity`` — a Task row leaves the active set the instant it
    reaches a terminal status. In-cycle pins made after this snapshot are covered
    separately by the caller's ``runner_pins_this_cycle`` counter.

    Returns ``{runner_id: count}`` with an entry for EVERY passed runner id (0 for
    an idle runner with no active tasks) on a successful query, so a populated dict
    unambiguously reflects the query result. Best-effort: on a query error returns
    an EMPTY dict — distinguishable from "all idle" precisely because success seeds
    every id — which the caller treats as "query failed, do not inject the
    authoritative count", so ``_runner_active_task_count`` falls back to the
    heartbeat ``active_task_ids`` hint instead of reading a false 0.
    """
    if not runner_ids:
        return {}
    vm_id_by_runner = {f"runner:{runner_id}": runner_id for runner_id in runner_ids}
    try:
        rows = (
            await session.execute(
                select(Task.vm_instance_id)
                .where(
                    Task.vm_instance_id.in_(list(vm_id_by_runner.keys())),
                    Task.status.in_(
                        [TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]
                    ),
                )
            )
        ).all()
    except Exception:
        logger.exception("[RUNNERS] Failed to load runner active-task counts")
        return {}

    # Seed every known runner at 0 so a SUCCESSFUL query returns an entry for each
    # runner id. This makes an empty return dict mean UNAMBIGUOUSLY "query failed"
    # (see the except above), rather than being indistinguishable from "all idle" —
    # the ambiguity that made a partial DB failure inject a false 0 everywhere.
    by_runner: dict[uuid.UUID, int] = dict.fromkeys(runner_ids, 0)
    for (vm_instance_id,) in rows:
        runner_id = vm_id_by_runner.get(vm_instance_id)
        if runner_id is None:
            continue
        by_runner[runner_id] = by_runner.get(runner_id, 0) + 1
    return by_runner


async def _try_runner_dispatch(
    session: AsyncSession,
    task: Task,
    active_runners_cache: dict[uuid.UUID, list[LocalRunner | RunnerDispatchSnapshot]],
    runner_pins_this_cycle: dict[uuid.UUID, int] | None = None,
) -> bool:
    """Pin a QUEUED task to an eligible registered runner, if one exists.

    Returns True when the task was pinned (PROVISIONING, vm_instance_id +
    context['runner_target_id'] set, committed) — the caller then skips the
    warm-pool and GCP dispatch paths for this task. Returns False when no runner
    is eligible, leaving every existing dispatch path to run exactly as before.

    ``runner_pins_this_cycle`` is a ``{runner_id: pins_so_far}`` counter the
    caller resets ONCE per dispatch cycle (so it counts only pins made this
    pass). It is consulted in the capacity check and incremented on a successful
    pin so a burst of QUEUED tasks in one cycle never over-pins a single idle
    runner beyond its max_concurrent pull capacity (the heartbeat-reported active
    count lags pins by a heartbeat, so it alone reads 0 for every iteration).

    Driven entirely by runner registration — no LOCAL_MODE flag is consulted.
    When omitted (single-call / test callers), it defaults to a fresh per-call
    counter, so a lone dispatch behaves exactly as before.
    """
    if runner_pins_this_cycle is None:
        runner_pins_this_cycle = {}
    ctx = task.context or {}
    # Already pinned by a prior cycle — leave it for the runner to pull.
    if ctx.get("runner_target_id"):
        return False
    payload = task.first_payload or {}
    task_type = payload.get("type", "")
    # A declared accelerator (flat or nested compute_experiment form) restricts
    # eligibility to GPU runners. Read generically so the gate is uniform; agent
    # task types simply carry no accelerator and are unaffected.
    accelerator = payload.get("accelerator") or (
        payload.get("compute_experiment") or {}
    ).get("accelerator")
    # A declared container_image restricts eligibility to runners with a
    # container isolation backend. Read flat and nested, mirroring accelerator.
    container_image = payload.get("container_image") or (
        payload.get("compute_experiment") or {}
    ).get("container_image")
    # A declared vram_gb restricts eligibility to runners with sufficient free VRAM.
    _vram_raw = payload.get("vram_gb") or (
        payload.get("compute_experiment") or {}
    ).get("vram_gb")
    vram_gb = float(_vram_raw) if _vram_raw is not None else None
    runner = _select_eligible_runner(
        active_runners_cache,
        task.project_id,
        task_type,
        accelerator,
        runner_pins_this_cycle,
        preferred_runner_target_id=ctx.get("preferred_runner_target_id"),
        dispatch_seed=task.id.int,
        container_image=container_image,
        vram_gb=vram_gb,
    )
    if runner is None:
        return False

    new_ctx = dict(ctx)
    new_ctx["runner_target_id"] = str(runner.id)
    new_ctx["runner_hostname"] = runner.hostname
    # A task that was held (enforce-local) and is now pinned must not carry the
    # stale wait stamp — otherwise the bounded-timeout sweep could fail it after
    # it has already been claimed by a runner.
    new_ctx.pop("awaiting_local_runner_since", None)
    new_ctx.pop("awaiting_container_runner_since", None)
    new_ctx.pop(STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY, None)
    new_ctx.pop("runner_pin_hold", None)
    task.context = new_ctx
    task.vm_instance_id = f"runner:{runner.id}"
    task.status = TaskStatus.PROVISIONING
    task.vm_provisioned_at = _utcnow()
    # Pinning moves the task out of the crash-prone prep window; clear the
    # contained-crash counter so the same commit persists it. Imported lazily
    # because ``_clear_dispatch_prep_failures`` stays in ``app.orchestrator``,
    # which imports this module at load time (re-export) — a top-level import
    # here would be a load-time cycle.
    from app.orchestrator import _clear_dispatch_prep_failures

    _clear_dispatch_prep_failures(task)
    session.add(task)
    try:
        await session.commit()
    except Exception:
        logger.exception(
            "[RUNNERS] Failed to commit runner pin for task %s", task.id
        )
        await session.rollback()
        # The rollback expired every session-bound ORM instance (independent of
        # expire_on_commit=False). A subsequent attribute read on ``task`` in
        # the dispatch loop (e.g. ``_hold_strategic_local_runner_task`` at
        # orchestrator.py:2842) would then fire a sync lazy-load from async
        # context and raise MissingGreenlet, quarantining a healthy task.
        # Re-pickling from the DB is async-safe because ``refresh()`` goes
        # through the session's async API, and the rollback has already opened
        # a new implicit transaction for the read.
        try:
            await session.refresh(task)
        except Exception:
            logger.exception(
                "[RUNNERS] Failed to refresh task %s after rollback", task.id
            )
        return False
    # Only charge the in-cycle pin slot once the pin is durably committed — a
    # rolled-back pin above must not consume the runner's per-cycle capacity.
    runner_pins_this_cycle[runner.id] = runner_pins_this_cycle.get(runner.id, 0) + 1
    logger.info(
        "[RUNNERS] Pinned task %s (type=%s) to runner %s",
        task.id,
        task_type,
        runner.id,
    )
    # If this task was held >15 min waiting for a runner, the hold alert that
    # flagged the stall is now stale — dismiss it. Best-effort: a resolve
    # failure must never abort a successful dispatch.
    try:
        from app.alert_dispatch import (
            _resolve_container_runner_hold_alert,
            _resolve_local_runner_hold_alert,
        )

        await _resolve_local_runner_hold_alert(session, task.id)
        await _resolve_container_runner_hold_alert(session, task.id)
    except Exception:
        logger.exception(
            "[RUNNERS] Failed to dismiss hold alert for task %s (non-fatal)", task.id
        )
    return True


def _task_requires_strategic_local_runner(task: Task) -> bool:
    payload = task.first_payload or {}
    task_type = str(payload.get("type") or "")
    return (
        task_type in STRATEGIC_TASK_TYPES
        and bool((task.context or {}).get(STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY))
    )


async def _hold_strategic_local_runner_task(
    session: AsyncSession,
    task: Task,
) -> bool:
    """Keep a local-required Strategic task queued instead of cloud-falling back."""

    if not _task_requires_strategic_local_runner(task):
        return False

    ctx = dict(task.context or {})
    changed = ctx.pop("runner_target_id", None) is not None
    if not ctx.get(STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY):
        ctx[STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY] = _utcnow().isoformat()
        changed = True
    if task.vm_instance_id is not None:
        task.vm_instance_id = None
        changed = True
    if task.vm_provisioned_at is not None:
        task.vm_provisioned_at = None
        changed = True

    intent_reason = str(
        ctx.get(STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY) or "workbench_continuous"
    )
    reason = (
        "Strategic workbench is in continuous local mode; waiting for a live "
        "Strategic-capable local runner instead of dispatching to cloud."
    )
    if task.latest_status_message != reason:
        task.latest_status_message = reason
        changed = True

    # Identity/status snapshot taken BEFORE the commit/rollback below. The
    # no-change branch calls ``session.rollback()``, which expires every
    # session-bound ORM instance even under ``expire_on_commit=False`` (that
    # setting governs commit, not rollback). A subsequent attribute read on
    # ``task`` (e.g. ``task.id`` in the trailing log line) would then fire a
    # sync lazy-load — ``_load_expired`` → asyncpg ping → ``await_only`` with
    # no active greenlet — and raise MissingGreenlet, deterministically
    # quarantining the held task. Reading into locals first avoids the reload.
    task_id_str = str(task.id)
    project_id_str = str(task.project_id)
    task_status = task.status
    if changed:
        task.context = ctx
        session.add(task)
        await session.commit()
        try:
            from app.broadcast import broadcast_to_project

            await broadcast_to_project(
                project_id_str,
                "task_updated",
                {
                    "task_id": task_id_str,
                    "status": task_status,
                    "message": reason,
                },
            )
        except Exception:
            logger.exception(
                "[RUNNERS] task_updated broadcast failed for held Strategic "
                "local-runner task %s",
                task_id_str,
            )
    else:
        await session.rollback()
    logger.info(
        "[RUNNERS] Holding Strategic task %s: continuous local mode requires "
        "a live Strategic-capable runner (reason=%s)",
        task_id_str,
        intent_reason,
    )
    return True


async def _try_strategic_local_required_runner_path(
    session: AsyncSession,
    task: Task,
    runners_by_project: dict[uuid.UUID, list[LocalRunner]],
    pins_this_cycle: dict[uuid.UUID, int],
) -> bool:
    """Dispatch or hold local-required Strategic tasks before cloud deferrals."""

    if not _task_requires_strategic_local_runner(task):
        return False
    if await _try_runner_dispatch(session, task, runners_by_project, pins_this_cycle):
        return True
    return await _hold_strategic_local_runner_task(session, task)


def _num_or_none(value) -> float | None:
    """Coerce ``value`` to a finite float, or None when it is not numeric.

    Mirrors the agent handler's ``numOrNull`` so the orchestrator validates the
    spend-cap / deadline launch fields exactly as the agent did (rejecting missing
    keys, JSON ``null``, non-numeric strings, NaN/inf) rather than crashing on a
    bare ``float(payload[...])`` or silently clamping a bad value into a billable
    job.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            f = float(value)
        except ValueError:
            return None
    else:
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return f


def _payload_runner_target(payload: dict) -> tuple[str | None, bool]:
    """Extract a launch-contract runner pin as ``(runner_id, strict)``.

    Reads ``runner_target: {runner_id, strict}`` flat or nested under
    ``compute_experiment`` (mirroring the accelerator / container_image reads).
    Returns ``(None, False)`` when absent or malformed — dispatch treats a
    malformed pin as no pin (the authoring contract validator rejects malformed
    shapes for operator launches; agent-authored payloads degrade soft).
    """
    target = payload.get("runner_target") or (
        payload.get("compute_experiment") or {}
    ).get("runner_target")
    if not isinstance(target, dict):
        return None, False
    runner_id = target.get("runner_id")
    if not (isinstance(runner_id, str) and runner_id.strip()):
        return None, False
    return runner_id.strip(), bool(target.get("strict"))


_PIN_HOLD_DIGITS_RE = re.compile(r"\d+")


def _pin_hold_stable_key(record) -> tuple | None:
    """Change-comparison key for ``context['runner_pin_hold']`` records.

    The human-readable ``detail`` embeds per-cycle-mutable values (live load
    counts, heartbeat timestamps), so full-record equality would flip every
    dispatch cycle while a pinned runner flaps — rewriting the held task row
    each cycle and defeating the change-gated commit. The key keeps what
    identifies the hold STATE — runner id + reason code — plus the detail with
    digit runs collapsed, so a genuinely different hold (another gate, other
    wording) still rewrites while volatile numbers do not. Returns ``None``
    for a missing/malformed record so the first stamp always writes.
    """
    if not isinstance(record, dict):
        return None
    detail = record.get("detail")
    coarse_detail = (
        _PIN_HOLD_DIGITS_RE.sub("#", detail) if isinstance(detail, str) else None
    )
    return (record.get("runner_id"), record.get("reason_code"), coarse_detail)


async def _diagnose_strict_runner_pin_hold(
    session: AsyncSession,
    active_runners_cache: dict[uuid.UUID, list[LocalRunner | RunnerDispatchSnapshot]],
    project_id: uuid.UUID,
    pinned_runner_id: str,
    task_type: str,
    accelerator: str | None,
    container_image: str | None,
    runner_pins_this_cycle: dict[uuid.UUID, int],
) -> tuple[str, str]:
    """Explain why a strict-pinned runner cannot take this job RIGHT NOW.

    Returns ``(reason_code, detail)``. The reason must be truthful and specific
    (runner id + why), never a generic no-runner message: reason codes are
    ``at_capacity`` / ``task_type_not_allowed`` / ``missing_gpu`` /
    ``missing_container_runtime`` / ``not_eligible`` (live but failed another
    gate) for runners in the live cache, and ``not_found`` / ``not_enrolled`` /
    ``offline`` for runners that are not live. The pair is stamped into
    ``task.context['runner_pin_hold']`` and the hold status message so the
    admin UI can show exactly why nothing is dispatching.
    """
    candidates = active_runners_cache.get(project_id) or []
    snapshot = next(
        (r for r in candidates if str(r.id) == pinned_runner_id), None
    )
    if snapshot is not None:
        if not _runner_handles_task_type(snapshot, task_type):
            return (
                "task_type_not_allowed",
                f"runner does not accept task type {task_type!r} "
                "(operator task_types policy)",
            )
        pins = runner_pins_this_cycle.get(snapshot.id, 0)
        # Mirror _select_eligible_runner exactly: capacity is per CLASS
        # (compute_experiment checks the compute-job class, agent tasks the
        # agent class), so the diagnosed reason matches why selection skipped
        # this runner.
        if _runner_at_capacity(snapshot, pins, task_type):
            caps = snapshot.capabilities or {}
            if task_type == "compute_experiment":
                load = _runner_active_compute_job_count(snapshot) + pins
                limit = _runner_max_concurrent_compute(snapshot)
                limit_label = f"max_concurrent_compute={limit}"
            else:
                load = _runner_active_task_count(snapshot) + pins
                limit_label = f"max_concurrent={caps.get('max_concurrent')}"
            return (
                "at_capacity",
                f"runner is at capacity ({load} active+pinned vs {limit_label})",
            )
        if accelerator and not (snapshot.capabilities or {}).get("has_gpu"):
            return (
                "missing_gpu",
                f"job declares accelerator {accelerator!r} but the runner does "
                "not advertise has_gpu",
            )
        if container_image and not _runner_has_container_capability(snapshot):
            return (
                "missing_container_runtime",
                f"job declares container_image {container_image!r} but the "
                "runner advertises no container isolation backend",
            )
        return ("not_eligible", "runner failed dispatch eligibility this cycle")

    # Not in the live cache — distinguish unknown / cross-project / offline via
    # the runner row. This path runs only while a strict pin is holding (rare,
    # once per dispatch cycle for the held task), so the extra read is cheap.
    try:
        runner_uuid = uuid.UUID(pinned_runner_id)
    except ValueError:
        return ("not_found", "runner_target.runner_id is not a valid runner id")
    row = await session.get(LocalRunner, runner_uuid)
    if row is None:
        return ("not_found", "no runner with this id is registered")
    if row.project_id != project_id:
        return (
            "not_enrolled",
            "runner is enrolled in a different project — a pin must name one "
            "of this project's runners",
        )
    last_seen = (
        row.last_seen_at.isoformat() if row.last_seen_at is not None else "never"
    )
    return (
        "offline",
        f"runner (host {row.hostname or 'unknown'}) is not live — "
        f"status={row.status!r}, last heartbeat {last_seen}; it must be active "
        f"and heartbeating within {_RUNNER_LIVENESS_SECONDS}s to claim work",
    )


async def _lookup_runner_hostname(
    session: AsyncSession, project_id: uuid.UUID, runner_id: str | None
) -> str | None:
    """Best-effort hostname for a runner id owned by ``project_id`` (else None).

    Used only to build an operator-legible "waiting for <host>" hold message — a
    lookup miss (offline/deleted/unknown/unparseable id) degrades to the raw id,
    never raising, so a display concern can never abort a dispatch commit.
    """
    if not runner_id:
        return None
    try:
        parsed = uuid.UUID(str(runner_id).strip())
    except (ValueError, TypeError, AttributeError):
        return None
    try:
        runner = await session.get(LocalRunner, parsed)
    except Exception:
        logger.exception(
            "[RUNNERS] hostname lookup failed for runner %s", runner_id
        )
        return None
    if runner is None or runner.project_id != project_id:
        return None
    return runner.hostname


def _select_eligible_runner(
    active_runners_cache: dict[uuid.UUID, list[LocalRunner | RunnerDispatchSnapshot]],
    project_id: uuid.UUID,
    task_type: str,
    accelerator: str | None,
    runner_pins_this_cycle: dict[uuid.UUID, int],
    preferred_runner_target_id: str | None = None,
    dispatch_seed: int = 0,
    container_image: str | None = None,
    forced_runner_target_id: str | None = None,
    vram_gb: float | None = None,
) -> "LocalRunner | RunnerDispatchSnapshot | None":
    """Pick the least-loaded eligible runner for a task/job, or None.

    Eligibility: the runner handles ``task_type`` (its advertised allowlist) and is
    not at its per-cycle pull capacity; when an ``accelerator`` is declared it must
    be VERIFIED GPU-capable via :func:`_runner_gpu_capable` — the self-reported
    ``has_gpu`` bool cross-checked against the runner's own GPU telemetry, so a
    mis-set flag cannot silently defeat the GPU capability gate (a CPU runner, or one
    whose telemetry contradicts its ``has_gpu`` claim, cannot provide a GPU).  When a
    ``container_image`` is declared the runner must advertise a container isolation
    backend (docker, podman, etc.) — the orchestrator will not route a
    container-image-dependent job to a runner whose isolation support is unknown.
    A preferred runner wins only after those eligibility checks pass; otherwise
    selection prefers the least EFFECTIVE load — heartbeat-reported active count
    plus pins already made to the runner this cycle — so a same-cycle burst spreads
    across a fleet instead of stacking onto one runner.

    ``forced_runner_target_id`` is an operator HARD pin (distinct from the soft
    ``preferred_runner_target_id`` hint): when set, candidate eligibility is
    restricted to ONLY that runner id and the function returns ``None`` if that one
    runner is not eligible this cycle — it NEVER falls through to the least-loaded
    selection or to a different runner. The caller (``_dispatch_compute_experiment_directly``)
    then HOLDS the task QUEUED rather than dispatching it elsewhere.

    When multiple runners are equally loaded, ``dispatch_seed`` (derived from the
    dispatched task/job UUID) breaks ties deterministically so repeated dispatch
    bursts rotate across the fleet rather than always landing on the same runner.

    Shared by agent-task dispatch (:func:`_try_runner_dispatch`) and direct
    compute-job dispatch (:func:`_dispatch_compute_experiment_directly`) so both use
    a single eligibility / least-loaded rule.
    """
    candidates = active_runners_cache.get(project_id) or []
    # Operator HARD pin: restrict the candidate pool to ONLY the forced runner id
    # BEFORE any eligibility check. If that runner is offline / at capacity / the
    # wrong task-type / lacks a required GPU it simply falls out of ``eligible``
    # below and the function returns None — it never spills over to another runner.
    if forced_runner_target_id:
        forced_id = str(forced_runner_target_id).strip()
        candidates = [r for r in candidates if str(r.id) == forced_id]
    pins = runner_pins_this_cycle
    eligible = [
        r
        for r in candidates
        if _runner_handles_task_type(r, task_type)
        and (task_type == "compute_experiment" or _runner_agent_available(r))
        and not _runner_at_capacity(r, pins.get(r.id, 0), task_type)
    ]
    if accelerator:
        # Trust VERIFIED GPU capability, not the bare self-reported has_gpu bool: a
        # mis-set flag would silently defeat this gate and win GPU jobs the runner
        # cannot run. _runner_gpu_capable cross-checks has_gpu against the runner's
        # own nvidia-smi telemetry (gpu_count/gpu_names) and disbelieves a claim its
        # telemetry contradicts. Fail-safe: a runner that cannot be verified
        # GPU-capable falls out of `eligible` and the task holds for real hardware.
        eligible = [r for r in eligible if _runner_gpu_capable(r)]
    if container_image:
        eligible = [r for r in eligible if _runner_has_container_capability(r)]
    if vram_gb is not None and vram_gb > 0:
        required_mib = int(vram_gb * 1024)
        eligible = [
            r
            for r in eligible
            if _runner_has_free_vram(r, required_mib)
        ]
    if not eligible:
        return None
    if preferred_runner_target_id:
        preferred_runner_id = str(preferred_runner_target_id).strip()
        preferred = next(
            (r for r in eligible if str(r.id) == preferred_runner_id),
            None,
        )
        if preferred is not None:
            return preferred
    # Deprioritize degraded runners (consecutive_job_failures at threshold) so
    # healthy runners are always preferred; degraded runners remain eligible as
    # a fallback so they can receive a job and reset their failure streak.
    # Sort by per-class load so compute tasks sort by compute load and agent tasks
    # sort by agent load — preventing one class from starving the other.
    # Among equally-loaded runners the XOR of the runner id with the dispatch
    # seed (task UUID int) acts as a stable per-task offset so different tasks
    # route to different runners instead of always picking the same first entry.
    is_compute = task_type == "compute_experiment"

    def _dispatch_sort_key(r: "LocalRunner | RunnerDispatchSnapshot") -> tuple:
        failures = getattr(r, "consecutive_job_failures", 0) or 0
        is_degraded = int(failures >= _RUNNER_DEGRADED_FAILURE_THRESHOLD)
        if is_compute:
            load = _runner_active_compute_job_count(r) + pins.get(r.id, 0)
        else:
            load = _runner_active_task_count(r) + pins.get(r.id, 0)
        tiebreak = r.id.int ^ dispatch_seed
        return (is_degraded, load, tiebreak)

    return min(eligible, key=_dispatch_sort_key)


def _build_compute_launch_kwargs(
    payload: dict, *, command: str, spend_cap_usd: float, deadline_seconds: int
) -> dict:
    """Map a ``compute_experiment`` task payload to ``create_compute_job`` kwargs.

    Server-side mirror of the agent handler's ``buildLaunchBody``: forwards only the
    fields that are present so backend defaults apply, so a task launched directly
    by the orchestrator produces the same ComputeJob the agent's
    ``POST /api/agent/compute-jobs`` used to create. ``arms``/``declared_metric`` are
    intentionally NOT forwarded — the multi-arm command is already synthesized into
    ``payload['command']`` at task creation (``compute_experiment_contract``), exactly
    as the agent saw it; ``declared_metric`` drives the platform pre-flight smoke, not
    ``create_compute_job``. The validated ``command``/``spend_cap_usd``/``deadline_seconds``
    are passed in by the caller (already range-checked).
    """
    def _present(value) -> bool:
        return isinstance(value, str) and value.strip() != ""

    kwargs: dict = {
        "command": command,
        "spend_cap_usd": spend_cap_usd,
        "deadline_seconds": deadline_seconds,
    }
    if _present(payload.get("machine_type")):
        kwargs["machine_type"] = payload["machine_type"]
    if _present(payload.get("cwd")):
        kwargs["cwd"] = payload["cwd"]
    if isinstance(payload.get("env"), dict):
        kwargs["env"] = payload["env"]
    if _present(payload.get("backend_name")):
        kwargs["backend_name"] = payload["backend_name"]
    if _present(payload.get("provisioning_model")):
        kwargs["provisioning_model"] = payload["provisioning_model"]
    # Accelerator: accept the nested compute_experiment form too, matching the
    # runner-selection gate and _try_runner_dispatch (a flat-only read would treat
    # a nested-accelerator GPU job as CPU and drop the accelerator).
    accelerator = payload.get("accelerator") or (
        payload.get("compute_experiment") or {}
    ).get("accelerator")
    if _present(accelerator):
        kwargs["accelerator"] = accelerator
    # Live-proof: accept the live_release_* / requires_live_release aliases too,
    # mirroring the agent's buildLaunchBody so a contract using either form gets the
    # same live-release enforcement.
    live_proof = payload.get("live_proof")
    if live_proof is None:
        live_proof = payload.get("live_release_proof")
    if live_proof is None:
        live_proof = payload.get("requires_live_release")
    if live_proof is not None:
        kwargs["live_proof"] = live_proof
    live_proof_marker = payload.get("live_proof_marker") or payload.get(
        "live_release_proof_marker"
    )
    if _present(live_proof_marker):
        kwargs["live_proof_marker"] = live_proof_marker
    if _present(payload.get("setup_step")):
        kwargs["setup_step"] = payload["setup_step"]
    if _present(payload.get("container_image")):
        kwargs["container_image"] = payload["container_image"]
    if isinstance(payload.get("container_registry_auth"), dict):
        kwargs["container_registry_auth"] = payload["container_registry_auth"]
    if payload.get("allow_host_fallback"):
        kwargs["allow_host_fallback"] = bool(payload["allow_host_fallback"])
    # No-progress watchdog window (peer of runtime deadline). Forwarded when set so
    # a stalled job is cut early; create_compute_job validates/clamps it. Integer
    # field, so it uses a plain presence check rather than the string `_present`.
    if payload.get("no_progress_timeout_seconds") is not None:
        kwargs["no_progress_timeout_seconds"] = payload["no_progress_timeout_seconds"]
    # Cross-job checkpoint resume: forward the optional predecessor reference so the
    # new job rehydrates that job's durable bundle before its command runs.
    # create_compute_job does the authoritative same-project + bundle-exists check.
    input_artifact_job_id = payload.get("input_artifact_job_id") or (
        payload.get("compute_experiment") or {}
    ).get("input_artifact_job_id")
    if _present(input_artifact_job_id):
        kwargs["input_artifact_job_id"] = input_artifact_job_id
    # declared_metric names the contract metric the no-progress watchdog watches
    # for a stall. Accept the nested compute_experiment form too, mirroring how the
    # accelerator is read above. (The multi-arm command is still synthesized at task
    # creation; this only forwards the label so the watchdog has a metric to track.)
    declared_metric = payload.get("declared_metric") or (
        payload.get("compute_experiment") or {}
    ).get("declared_metric")
    if _present(declared_metric):
        kwargs["declared_metric"] = declared_metric
    return kwargs


async def _fail_compute_experiment_task(
    session: AsyncSession, task: Task, failure_class: str, message: str
) -> str:
    """Terminal-FAIL a ``compute_experiment`` whose launch failed at dispatch.

    Mirrors the terminal-fail pattern used elsewhere in dispatch (status + message +
    failure_category + failure alert + ``record_task_result``, committed in one
    batch) so the failure is classified for direction rollups / research dedup. Used
    only for genuine launch failures (misconfig, non-recoverable preconditions) with
    ``failure_class="compute_launch_failed"`` — recoverable live-release preconditions
    take the BLOCKED path in the caller, never this one. Returns ``"failed"``.
    """
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.broadcast import broadcast_to_project
    from app.failure_classification import persist_failure_category
    from app.routers.agent_status import record_task_result

    task.status = TaskStatus.FAILED
    task.latest_status_message = message[:500]
    persist_failure_category(task, failure_class)
    session.add(task)
    try:
        await _maybe_create_failure_alert(task, session, message[:200])
    except Exception:
        logger.exception(
            "[ORCHESTRATOR] Failed to create compute-launch failure alert for task %s",
            task.id,
        )
    record_task_result(
        success=False,
        failure_class=failure_class,
        task_id=str(task.id),
        task_type="compute_experiment",
    )
    await session.commit()
    try:
        await broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {
                "task_id": str(task.id),
                "status": TaskStatus.FAILED,
                "message": message[:200],
            },
        )
    except Exception:
        logger.exception(
            "[ORCHESTRATOR] task_updated broadcast failed for compute_experiment %s",
            task.id,
        )
    logger.warning(
        "[ORCHESTRATOR] compute_experiment %s launch failed (%s): %s",
        task.id,
        failure_class,
        message[:200],
    )
    return "failed"


async def _dispatch_compute_experiment_directly(
    session: AsyncSession,
    task: Task,
    active_runners_cache: dict[uuid.UUID, list[LocalRunner | RunnerDispatchSnapshot]],
    runner_pins_this_cycle: dict[uuid.UUID, int],
) -> str:
    """Launch a ``compute_experiment``'s ComputeJob and PARK the task — no agent half.

    The orchestrator does the compute_experiment agent leg's mechanical work itself:
    select an eligible runner (so a GPU job pins to operator hardware), create the
    ComputeJob, and park the task on the existing ``compute_job_terminal`` wake. The
    backend monitor + ``_handle_compute_experiment_completion`` then finalize the task
    exactly as on the agent path — only *who launches* changes.

    Runner selection reuses the same eligibility as :func:`_try_runner_dispatch`
    (handles the type, not at capacity, GPU-advertising when an accelerator is
    declared); the chosen runner is stamped into ``task.context['runner_target_id']``
    so ``create_compute_job`` routes the job to the runner-subprocess backend and the
    runner's ``next-compute-job`` poll claims it. Cloud fallback is preserved: with no
    eligible runner and no enforce-local policy, ``create_compute_job`` picks the
    GCP/local backend as before.

    Returns:
        ``"launched"`` — ComputeJob created, task PARKED on the job-terminal wake.
        ``"failed"``   — launch misconfigured / non-recoverable precondition; task
                         terminal-FAILED.
        ``"blocked"``  — a recoverable live-release precondition (release not yet
                         serving-confirmed); task BLOCKED, auto-unblock sweep re-queues
                         it once the deploy confirms (not a failure, no retry burn).
        ``"held"``     — held QUEUED (``awaiting_local_runner_since`` stamped; the
                         existing bounded sweep terminal-fails a hold that outlives
                         the wait window). Two hold causes: ``require_local_runner``
                         set and no live runner, or a strict ``runner_target`` pin
                         whose runner is not currently eligible (the latter also
                         stamps ``context['runner_pin_hold']`` with the
                         machine-readable reason).
    Only ``"launched"`` and ``"failed"`` count as a dispatch; ``"blocked"``/``"held"``
    leave the task waiting. The caller owns the dispatch loop; this function commits
    its own outcome.
    """
    from app.broadcast import broadcast_to_project
    from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME
    from app.compute_jobs import (
        COMPUTE_JOB_TERMINAL_WAKE_KIND,
        ComputeLaunchPreconditionFailed,
        create_compute_job,
    )
    from app.models import WakeAction
    from app.task_lifecycle import park_task

    # resolve_project_require_local_runner is imported at module scope (see the
    # top-of-file app.resource_envelope import).

    payload = task.first_payload or {}

    # ── Validate launch fields up front (mirror the agent handler) ─────────────
    # Fail loudly + cleanly on a misconfigured task instead of crash-looping on a
    # bare float(payload[...]) (KeyError/TypeError) or silently clamping a zero or
    # negative cap into a real billable job.
    command = str(payload.get("command") or "").strip()
    spend_cap = _num_or_none(payload.get("spend_cap_usd"))
    deadline_seconds = _num_or_none(payload.get("deadline_seconds"))
    config_errors: list[str] = []
    if not command:
        config_errors.append("command is required")
    if spend_cap is None or spend_cap <= 0:
        config_errors.append("spend_cap_usd must be a positive number")
    if deadline_seconds is None or deadline_seconds <= 0:
        config_errors.append("deadline_seconds must be a positive number")
    if config_errors:
        return await _fail_compute_experiment_task(
            session,
            task,
            "compute_launch_failed",
            "Compute experiment misconfigured: " + "; ".join(config_errors),
        )

    # Accelerator (flat or nested compute_experiment form) gates runner eligibility.
    accelerator = payload.get("accelerator") or (
        payload.get("compute_experiment") or {}
    ).get("accelerator")
    # Container-image (flat or nested) gates runner eligibility on container
    # isolation backend capability.
    container_image = payload.get("container_image") or (
        payload.get("compute_experiment") or {}
    ).get("container_image")
    # vram_gb (flat or nested) gates runner eligibility on free GPU memory.
    _vram_raw = payload.get("vram_gb") or (
        payload.get("compute_experiment") or {}
    ).get("vram_gb")
    vram_gb = float(_vram_raw) if _vram_raw is not None else None

    # Launch-contract runner pin (operator or agent authored). The pin id feeds
    # the existing preferred-runner seam; ``strict`` additionally forbids any
    # other runner (hold-with-reason below instead of falling through).
    pin_runner_id, pin_strict = _payload_runner_target(payload)

    # Operator HARD pin (distinct from the soft preferred hint): this job may run
    # ONLY on this runner id. A forced target that is offline/ineligible HOLDS the
    # task — it never falls back to another runner or to cloud (handled below).
    forced_runner_target_id = (task.context or {}).get(
        FORCED_RUNNER_TARGET_CONTEXT_KEY
    )

    # ── Select an eligible runner (shared rule with _try_runner_dispatch) ──────
    runner = _select_eligible_runner(
        active_runners_cache,
        task.project_id,
        "compute_experiment",
        accelerator,
        runner_pins_this_cycle,
        preferred_runner_target_id=pin_runner_id
        or (task.context or {}).get("preferred_runner_target_id"),
        dispatch_seed=task.id.int,
        container_image=container_image,
        forced_runner_target_id=forced_runner_target_id,
        vram_gb=vram_gb,
    )

    # ── Strict runner pin: ONLY the pinned runner may take this job ────────────
    # When the pinned runner is not currently eligible, hold QUEUED with a
    # truthful, machine-readable reason (runner id + why) — never dispatch to
    # another runner or the cloud, and never reuse the generic require-local
    # wording. The ``awaiting_local_runner_since`` stamp reuses the existing
    # bounded-wait sweep (15-min operator alert, 6h terminal fail), whose
    # timeout message is pin-aware (see _fail_stale_local_runner_holds).
    if (
        pin_strict
        and pin_runner_id
        and (runner is None or str(runner.id) != pin_runner_id)
    ):
        reason_code, detail = await _diagnose_strict_runner_pin_hold(
            session,
            active_runners_cache,
            task.project_id,
            pin_runner_id,
            "compute_experiment",
            accelerator,
            container_image,
            runner_pins_this_cycle,
        )
        message = (
            f"runner_target hold: strict pin to runner {pin_runner_id} cannot "
            f"dispatch — {reason_code}: {detail}"
        )[:500]
        ctx = dict(task.context or {})
        changed = ctx.pop("runner_target_id", None) is not None  # drop stale pin
        if not ctx.get("awaiting_local_runner_since"):
            ctx["awaiting_local_runner_since"] = _utcnow().isoformat()
            changed = True
        hold_record = {
            "runner_id": pin_runner_id,
            "strict": True,
            "reason_code": reason_code,
            "detail": detail,
        }
        # Change-gate on the STABLE key only (runner id + reason code + detail
        # with volatile numbers stripped): the raw detail carries per-cycle
        # values (live load counts, heartbeat timestamps), so full-record
        # comparison would rewrite the task row every cycle for a flapping
        # pinned runner. The stamped record and status message keep the
        # volatile detail from the first write of each stable hold state.
        if _pin_hold_stable_key(ctx.get("runner_pin_hold")) != _pin_hold_stable_key(
            hold_record
        ):
            ctx["runner_pin_hold"] = hold_record
            task.latest_status_message = message
            changed = True
        if changed:
            task.context = ctx
            session.add(task)
            await session.commit()
        logger.info(
            "[RUNNERS] Holding compute_experiment %s: strict runner_target pin "
            "to %s not dispatchable (%s: %s)",
            task.id,
            pin_runner_id,
            reason_code,
            detail,
        )
        return "held"

    # ── Forced-runner hold: operator hard-pinned this job to a specific runner ──
    # When that runner is not eligible this cycle (offline / at capacity / wrong
    # task-type / lacks a required GPU) HOLD QUEUED with an honest, host-named
    # reason — NEVER fall back to a different runner or to cloud, and independent
    # of the project's enforce-local / container-image policy (checked first so a
    # forced offline target holds regardless of those). The forced pin itself is
    # preserved in context; only a stale CLAIMED ``runner_target_id`` is dropped.
    if runner is None and forced_runner_target_id:
        forced_id = str(forced_runner_target_id).strip()
        forced_hostname = await _lookup_runner_hostname(
            session, task.project_id, forced_id
        )
        target_label = forced_hostname or forced_id
        hold_msg = (
            f"Waiting for forced runner {target_label}: this compute experiment is "
            "hard-pinned to that runner and will not run on any other runner or in "
            "the cloud until it is live and eligible."
        )
        ctx = dict(task.context or {})
        changed = ctx.pop("runner_target_id", None) is not None
        if not ctx.get(AWAITING_FORCED_RUNNER_CONTEXT_KEY):
            ctx[AWAITING_FORCED_RUNNER_CONTEXT_KEY] = _utcnow().isoformat()
            changed = True
        if task.latest_status_message != hold_msg:
            task.latest_status_message = hold_msg
            changed = True
        if changed:
            task.context = ctx
            session.add(task)
            await session.commit()
        logger.info(
            "[RUNNERS] Holding compute_experiment %s: forced runner %s not eligible "
            "this cycle (offline/at-capacity/ineligible) — not dispatching elsewhere",
            task.id,
            target_label,
        )
        return "held"

    project = await session.get(Project, task.project_id)
    require_local = bool(
        project is not None and resolve_project_require_local_runner(project)
    )

    # ── Enforce-local hold: require_local + no runner → keep QUEUED, never cloud ─
    if runner is None and require_local:
        ctx = dict(task.context or {})
        changed = ctx.pop("runner_target_id", None) is not None  # drop any stale pin
        if not ctx.get("awaiting_local_runner_since"):
            ctx["awaiting_local_runner_since"] = _utcnow().isoformat()
            changed = True
        if changed:
            task.context = ctx
            session.add(task)
            await session.commit()
        logger.info(
            "[RUNNERS] Holding compute_experiment %s: require_local_runner set, "
            "no live eligible runner",
            task.id,
        )
        return "held"

    # ── Container-image hold: image declared + registered runners but none
    # container-capable → keep QUEUED with an honest reason, never cloud-dispatch.
    # Only fires when the project has at least one live runner (runners exist but
    # lack the runtime); when no runners are registered at all we fall through to
    # the cloud backend so the job is not silently wedged.
    if runner is None and container_image:
        project_runners = active_runners_cache.get(task.project_id) or []
        if project_runners:
            hold_msg = (
                "Waiting for a container-capable runner: job declares "
                f"container_image={container_image!r} but no live runner "
                "has docker_usable capability."
            )
            ctx = dict(task.context or {})
            changed = ctx.pop("runner_target_id", None) is not None
            if not ctx.get("awaiting_container_runner_since"):
                ctx["awaiting_container_runner_since"] = _utcnow().isoformat()
                changed = True
            if task.latest_status_message != hold_msg:
                task.latest_status_message = hold_msg
                changed = True
            if changed:
                task.context = ctx
                session.add(task)
                await session.commit()
            logger.info(
                "[RUNNERS] Holding compute_experiment %s: container_image declared "
                "(%r) but no container-capable runner available",
                task.id,
                container_image,
            )
            return "held"

    # ── Pin to the chosen runner; the job inherits it via task.context ─────────
    if runner is not None:
        ctx = dict(task.context or {})
        ctx["runner_target_id"] = str(runner.id)
        ctx["runner_hostname"] = runner.hostname
        ctx.pop("awaiting_local_runner_since", None)
        # A strict-pin hold that is now dispatching must drop its stale hold
        # reason — the pinned runner became eligible.
        ctx.pop("runner_pin_hold", None)
        ctx.pop("awaiting_container_runner_since", None)
        ctx.pop(AWAITING_FORCED_RUNNER_CONTEXT_KEY, None)
        task.context = ctx
        session.add(task)
    elif (task.context or {}).get("runner_target_id"):
        # No eligible runner this cycle (and not enforce-local): drop a STALE pin
        # left by a prior cycle so create_compute_job takes cloud fallback instead
        # of forcing the job onto a now-offline/ineligible runner that will never
        # pull it (it would sit QUEUED until the provisioning sub-deadline →
        # TIMED_OUT). _try_runner_dispatch avoids this by short-circuiting on an
        # already-set pin; the direct path clears it here.
        ctx = dict(task.context or {})
        ctx.pop("runner_target_id", None)
        task.context = ctx
        session.add(task)

    # Flush the runner pin (or its clearing) BEFORE create_compute_job. That call
    # re-locks the task with populate_existing=True, which overwrites any unflushed
    # in-session attribute change with the row as last persisted — so without this
    # flush the pin set above is silently discarded and a GPU job falls back to the
    # cloud backend instead of routing to the runner. (Relying on session autoflush
    # here is fragile; make it explicit.)
    await session.flush()

    # ── Launch the ComputeJob (server-side mirror of the agent handler) ────────
    launch_kwargs = _build_compute_launch_kwargs(
        payload,
        command=command,
        spend_cap_usd=float(spend_cap),
        deadline_seconds=int(deadline_seconds),
    )
    if runner is not None:
        # A runner was selected → the job runs on operator hardware. Pin the
        # runner-subprocess backend explicitly so create_compute_job does not first
        # derive — and, in a non-cloud env, reject — a tentative cloud GPU backend
        # before its runner_target_id override is reached. The flushed
        # runner_target_id in task.context still drives next-compute-job routing.
        launch_kwargs["backend_name"] = RUNNER_SUBPROCESS_BACKEND_NAME
    try:
        job = await create_compute_job(session, task, **launch_kwargs)
    except ComputeLaunchPreconditionFailed as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        msg = str(detail.get("message") or "compute launch precondition failed")
        # A live-release precondition is RECOVERABLE — the target release is just
        # not serving-confirmed yet. Enter the first-class BLOCKED state instead of
        # terminal-failing, mirroring the agent path's BLOCKED_PRECONDITION handling.
        if detail.get("error") == "live_release_precondition_failed":
            from app.task_blocked import enter_blocked_state

            # source="agent" so the unblock sweep RE-QUEUES this block: only
            # source=="agent" no-dependency blockers get the optimistic "retry"
            # path (blocked_task_unblock_sweeps.py); any other source stays
            # "waiting" forever — which would silently wedge the experiment. The
            # agent path surfaced this exact block as source="agent". Re-dispatch
            # here is cheap (no agent VM — just a re-entry into this function), so
            # the bounded optimistic-retry budget comfortably covers a normal
            # serving-confirm window.
            enter_blocked_state(
                task,
                waiting_on="precondition",
                reason=msg[:500],
                source="agent",
            )
            session.add(task)
            # Snapshot the broadcast/log scalars into plain locals BEFORE commit:
            # session.commit() expires all ORM state, so reading task.project_id/.id
            # afterwards lazy-loads from async context and raises MissingGreenlet on
            # asyncpg, quarantining a task that was correctly BLOCKED.
            _blocked_project_id = str(task.project_id)
            _blocked_task_id = str(task.id)
            await session.commit()
            try:
                await broadcast_to_project(
                    _blocked_project_id,
                    "task_updated",
                    {"task_id": _blocked_task_id, "status": TaskStatus.BLOCKED},
                )
            except Exception:
                logger.exception(
                    "[ORCHESTRATOR] task_updated broadcast failed for blocked "
                    "compute_experiment %s",
                    _blocked_task_id,
                )
            logger.info(
                "[ORCHESTRATOR] compute_experiment %s BLOCKED on live-release "
                "precondition (recoverable): %s",
                _blocked_task_id,
                msg[:200],
            )
            return "blocked"
        # Non-live-release preconditions (spend ceiling, hardware policy,
        # concurrency, local_runner_required) are genuine launch failures, not
        # recoverable blocks — label them ``compute_launch_failed`` exactly as the
        # agent path did. Using ``BLOCKED_PRECONDITION`` on a terminal FAILED task
        # would make a parent waiting on this child mis-read it as still in flight
        # (blocked_task_unblock_sweeps treats failure_category BLOCKED_PRECONDITION
        # as "waiting") and wedge.
        return await _fail_compute_experiment_task(
            session, task, "compute_launch_failed", msg
        )
    except ValueError as exc:
        return await _fail_compute_experiment_task(
            session, task, "compute_launch_failed", f"Compute job launch failed: {exc}"
        )

    # Charge the in-cycle pin slot only after a durable launch (mirrors
    # _try_runner_dispatch, so a same-cycle burst never over-pins one runner).
    if runner is not None:
        runner_pins_this_cycle[runner.id] = (
            runner_pins_this_cycle.get(runner.id, 0) + 1
        )
    # If this task was held >15 min waiting for a runner, the hold alert that
    # flagged the stall is now stale — dismiss it. Best-effort: a resolve
    # failure must never abort a successful dispatch.
    try:
        from app.alert_dispatch import (
            _resolve_container_runner_hold_alert,
            _resolve_local_runner_hold_alert,
        )

        await _resolve_local_runner_hold_alert(session, task.id)
        await _resolve_container_runner_hold_alert(session, task.id)
    except Exception:
        logger.exception(
            "[RUNNERS] Failed to dismiss hold alert for task %s (non-fatal)", task.id
        )

    # ── Park the task on the job-terminal wake (releases its slot now) ─────────
    meta = job.polling_metadata or {}
    park_deadline_seconds = meta.get("park_deadline_seconds")
    if not isinstance(park_deadline_seconds, (int, float)) or park_deadline_seconds <= 0:
        park_deadline_seconds = int(deadline_seconds) + 600
    deadline_at = _utcnow() + timedelta(seconds=int(park_deadline_seconds))
    await park_task(
        session,
        task,
        wake_source_kind=COMPUTE_JOB_TERMINAL_WAKE_KIND,
        wake_source_ref=str(job.id),
        wake_action=WakeAction.FINALIZE,
        deadline_at=deadline_at,
        reason=f"Parked on compute job {job.id} terminal",
        source_metadata={
            "job_id": str(job.id),
            "experiment": payload.get("experiment"),
        },
    )
    await session.commit()
    # Mirror the agent-status PARKED path's websocket broadcast so the board
    # reflects PARKED in real time instead of a stale QUEUED card until next poll.
    try:
        await broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(task.id), "status": TaskStatus.PARKED},
        )
    except Exception:
        logger.exception(
            "[ORCHESTRATOR] task_updated broadcast failed for parked "
            "compute_experiment %s",
            task.id,
        )
    logger.info(
        "[ORCHESTRATOR] Launched compute_experiment %s as job %s (%s); task PARKED",
        task.id,
        job.id,
        f"runner {runner.id}" if runner is not None else "cloud backend",
    )
    return "launched"


async def _deactivate_stale_runners(session: AsyncSession) -> None:
    """Mark runners that missed the heartbeat window as inactive.

    Keeps the dispatch cache query clean and surfaces stale runners in the admin
    listing. Any task pinned to a now-inactive runner that the runner never
    pulled (``started_at IS NULL``) is released back to QUEUED so the next
    dispatch cycle can re-pin it to a live runner, hold it (enforce-local, with
    the 6h safety-fail), or fall back to cloud. This recovery is required because
    the orphaned-task reaper intentionally no longer reaps awaiting-pull runner
    pins (they are not dead GCE VMs); without it a pin would wedge forever if its
    runner went offline in the pin->pull window.
    """
    from sqlalchemy import or_, update as _sa_update

    now = _utcnow()
    cutoff = now - timedelta(seconds=_RUNNER_LIVENESS_SECONDS)
    stale_rows = (
        await session.execute(
            select(
                LocalRunner.id,
                LocalRunner.project_id,
                LocalRunner.hostname,
                LocalRunner.capabilities,
                LocalRunner.last_seen_at,
            ).where(
                LocalRunner.status == "active",
                or_(
                    LocalRunner.last_seen_at.is_(None),
                    LocalRunner.last_seen_at < cutoff,
                ),
            )
        )
    ).all()
    stale_ids = [row[0] for row in stale_rows]
    if not stale_ids:
        return
    stale_counts_by_project: dict[uuid.UUID, int] = {}
    for row in stale_rows:
        project_id = row[1]
        stale_counts_by_project[project_id] = (
            stale_counts_by_project.get(project_id, 0) + 1
        )

    await session.execute(
        _sa_update(LocalRunner)
        .where(LocalRunner.id.in_(stale_ids))
        .values(status="inactive")
    )

    # Release unpulled pins on the now-inactive runners back to QUEUED. A pin the
    # runner never claimed has started_at IS NULL and vm_instance_id "runner:{id}"
    # (set together in _try_runner_dispatch); clearing the pin lets the dispatch
    # loop re-decide next cycle.
    stale_vm_ids = [f"runner:{rid}" for rid in stale_ids]
    pinned = (
        await session.execute(
            select(Task).where(
                Task.status == TaskStatus.PROVISIONING,
                Task.started_at.is_(None),
                Task.vm_instance_id.in_(stale_vm_ids),
            )
        )
    ).scalars().all()
    released_pins_by_project: dict[uuid.UUID, int] = {}
    for task in pinned:
        task.status = TaskStatus.QUEUED
        task.vm_instance_id = None
        task.vm_provisioned_at = None
        new_ctx = dict(task.context or {})
        new_ctx.pop("runner_target_id", None)
        task.context = new_ctx
        session.add(task)
        released_pins_by_project[task.project_id] = (
            released_pins_by_project.get(task.project_id, 0) + 1
        )
        logger.info(
            "[RUNNERS] Released unpulled pin for task %s from a now-inactive "
            "runner (back to QUEUED for re-dispatch)",
            task.id,
        )

    await session.commit()
    # Alert the operator on each newly-dead runner (the status flip above is the
    # active->inactive transition, so this fires exactly once per death; the
    # dedupe_key upsert is a second guard). Runs AFTER the commit so the alert /
    # webhook only announce a durably-recorded deactivation. Best-effort per
    # runner so an alert failure never aborts the sweep. Runner counts are tiny
    # (operator hardware), so the per-runner commit inside the helper is cheap.
    from app.alert_dispatch import _maybe_create_runner_deactivated_alert

    for row in stale_rows:
        runner_id, project_id, hostname, capabilities, last_seen_at = row
        caps = capabilities or {}
        active_work_count = 0
        for _work_key in ("active_task_ids", "active_compute_job_ids"):
            _ids = caps.get(_work_key)
            if isinstance(_ids, list):
                active_work_count += len(_ids)
        seconds_since_last_seen = (
            max(0, int((now - last_seen_at).total_seconds()))
            if last_seen_at is not None
            else None
        )
        try:
            await _maybe_create_runner_deactivated_alert(
                session,
                project_id=project_id,
                runner_id=runner_id,
                hostname=hostname,
                had_active_work=active_work_count > 0,
                active_work_count=active_work_count,
                seconds_since_last_seen=seconds_since_last_seen,
            )
        except Exception:
            logger.exception(
                "[RUNNERS] Failed to create runner-deactivated alert for %s",
                runner_id,
            )
            # Clear any half-staged alert so the next runner's creation starts
            # from a clean transaction (each helper call commits its own row).
            try:
                await session.rollback()
            except Exception:
                logger.exception("[RUNNERS] rollback after alert failure failed")
    for project_id, deactivated_count in stale_counts_by_project.items():
        try:
            await emit_workbench_refetch(
                project_id,
                resource="local_runner",
                action="deactivated_stale",
                refetch_surfaces=(SURFACE_STRATEGIC_SESSION, SURFACE_RUNTIME),
                summary_key="runners",
                summary={
                    "deactivated_count": deactivated_count,
                    "released_unpulled_pins": released_pins_by_project.get(
                        project_id, 0
                    ),
                },
            )
        except Exception:
            logger.exception(
                "[RUNNERS] Failed to emit stale-runner refetch for project %s",
                project_id,
            )
    # Escalate alerts for runners that have been inactive past the threshold.
    try:
        await _sweep_runner_deactivated_escalations(session)
    except Exception:
        logger.exception("[RUNNERS] Failed to sweep runner-deactivated escalations")


def _parse_local_runner_hold_timestamp(raw: object) -> datetime | None:
    try:
        stamped_at = datetime.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return None
    if stamped_at.tzinfo is not None:
        stamped_at = stamped_at.astimezone(timezone.utc).replace(tzinfo=None)
    return stamped_at


_STRATEGIC_LOCAL_FALLBACK_ALERT_TITLE = (
    "Strategic local runner offline — falling back to cloud"
)


async def _create_strategic_local_cloud_fallback_alert(
    task: Task,
    session: AsyncSession,
    message: str,
) -> None:
    """Stage a non-failure INFO alert when a Strategic local hold falls back to cloud.

    Distinct from ``_maybe_create_failure_alert`` (ERROR / failure semantics):
    re-dispatching an abandoned required-local Strategic task to the cloud is a
    graceful self-heal (the project keeps progressing), NOT a failure — so it
    carries INFO severity and never records a failed task result. Deduplicates on
    (task, stable title) so repeated sweep passes never create a second row.
    Mirrors ``alert_dispatch._maybe_create_spend_hold_alert``: the caller commits
    the session; this only stages the row and broadcasts ``alert_created``.
    """
    from app.models import Alert, AlertSeverity

    existing = await session.execute(
        select(Alert).where(
            Alert.task_id == task.id,
            Alert.title == _STRATEGIC_LOCAL_FALLBACK_ALERT_TITLE,
        )
    )
    if existing.scalars().first() is not None:
        return
    alert = Alert(
        project_id=task.project_id,
        severity=AlertSeverity.INFO,
        title=_STRATEGIC_LOCAL_FALLBACK_ALERT_TITLE,
        message=message,
        task_id=task.id,
    )
    session.add(alert)
    try:
        from app.broadcast import broadcast_to_project

        await broadcast_to_project(
            str(task.project_id),
            "alert_created",
            {
                "alert_id": str(alert.id),
                "severity": "INFO",
                "title": alert.title,
            },
        )
    except Exception:
        logger.exception(
            "[RUNNERS] alert_created broadcast failed for Strategic cloud "
            "fallback on task %s",
            task.id,
        )


def _strategic_local_hold_timeout_seconds() -> int:
    """Bounded wait (seconds) before a required-local Strategic hold falls back to cloud.

    Live value from the ``strategic_local_hold_timeout_seconds`` runtime setting
    (default 900s / 15 min), replacing the old 6h hold-then-FAIL for Strategic
    holds. Clamped defensively so a corrupted ``runtime_config_json`` never yields
    a zero/negative window that would re-dispatch a just-held task instantly.
    """
    try:
        value = int(_runtime_config.get("strategic_local_hold_timeout_seconds", 900))
    except (TypeError, ValueError):
        return 900
    return value if value > 0 else 900


async def _fail_stale_local_runner_holds(session: AsyncSession) -> int:
    """Resolve tasks held too long for a local runner.

    Two hold classes share the bounded-wait contract but resolve DIFFERENTLY:

    * **compute_experiment** (``context['awaiting_local_runner_since']`` or
      ``context['awaiting_container_runner_since']``): held QUEUED instead of
      cloud-provisioned when no live self-hosted runner claimed it (require-local)
      or no runner advertises ``docker_usable`` for an image-declaring job
      (container hold). A hold older than ``_LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS``
      (6h) is marked terminal-FAILED (PROVISION_FAILURE) — neither variant has a
      safe cloud fallback (each needs specific operator hardware/runtime), so
      failing is correct. Both emit an early-warning alert first.

    * **continuous local Strategic** (``context['awaiting_strategic_local_runner_since']``
      + ``context['strategic_local_runner_required']``): SAFETY self-heal. A hold
      older than ``strategic_local_hold_timeout_seconds`` (default 900s / 15 min)
      is NOT failed — instead the local-required pin is cleared and the task is
      returned to normal dispatch so it **falls back to CLOUD** and the project
      keeps making progress. This fixes "opened the workbench and left → project
      frozen": an abandoned session with a dead/absent runner recovers within the
      short lease window instead of failing tasks for 6h. The operator is still
      alerted, but at INFO/degraded severity (a graceful fallback, not a failure).

    Returns the number of tasks terminally FAILED (compute holds only); Strategic
    cloud-fallbacks are counted separately in the log line but are not failures.
    """
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.failure_classification import (
        PROVISION_FAILURE,
        persist_failure_category,
    )
    from app.routers.agent_status import record_task_result

    now = _utcnow()
    compute_cutoff = now - timedelta(seconds=_LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS)
    strategic_timeout = _strategic_local_hold_timeout_seconds()
    strategic_cutoff = now - timedelta(seconds=strategic_timeout)
    # JSON-path predicates differ across dialects (Postgres prod vs SQLite tests),
    # so select QUEUED tasks and filter the context stamp / payload type in
    # Python — the candidate set is tiny (only require-local projects ever stamp).
    rows = (
        await session.execute(
            select(Task).where(Task.status == TaskStatus.QUEUED)
        )
    ).scalars().all()

    failed = 0
    redispatched = 0
    for task in rows:
        ctx = task.context or {}
        task_type = str((task.first_payload or {}).get("type") or "")
        compute_stamp = ctx.get("awaiting_local_runner_since")
        container_stamp = ctx.get("awaiting_container_runner_since")
        strategic_stamp = ctx.get(STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY)
        is_compute_hold = (
            bool(compute_stamp)
            and task_type == "compute_experiment"
            and not task.archived
            and task.requires_approval is not True
        )
        # Container-image hold: an image-declaring job parked because no live
        # runner advertises docker_usable. Same bounded-wait contract as the
        # require-local compute hold (early-warning alert, then 6h terminal FAIL —
        # no safe cloud fallback), but distinct alert copy + failure reason so the
        # operator sees the accurate cause. Require-local takes precedence if a
        # stale local stamp somehow coexists.
        is_container_hold = (
            bool(container_stamp)
            and not is_compute_hold
            and task_type == "compute_experiment"
            and not task.archived
            and task.requires_approval is not True
        )
        is_strategic_hold = (
            bool(strategic_stamp)
            and task_type in STRATEGIC_TASK_TYPES
            and ctx.get(STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY) is True
            and not task.archived
            and task.requires_approval is not True
        )
        if not is_compute_hold and not is_container_hold and not is_strategic_hold:
            continue

        if is_strategic_hold:
            stamped = strategic_stamp
        elif is_container_hold:
            stamped = container_stamp
        else:
            stamped = compute_stamp
        stamped_at = _parse_local_runner_hold_timestamp(stamped)
        if stamped_at is None:
            continue
        cutoff = strategic_cutoff if is_strategic_hold else compute_cutoff
        if stamped_at > cutoff:
            # Within the full wait window — keep holding. For compute holds, emit a
            # proactive early-warning alert once the hold passes the shorter threshold
            # so the operator learns the hardware is offline in minutes, not 6h.
            if is_compute_hold or is_container_hold:
                alert_cutoff = now - timedelta(seconds=_LOCAL_RUNNER_HOLD_ALERT_SECONDS)
                if stamped_at <= alert_cutoff:
                    held_secs = int((now - stamped_at).total_seconds())
                    remaining_secs = max(
                        0, _LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS - held_secs
                    )
                    try:
                        if is_container_hold:
                            from app.alert_dispatch import (
                                _maybe_create_container_runner_hold_alert,
                            )

                            created = await _maybe_create_container_runner_hold_alert(
                                task,
                                session,
                                held_seconds=held_secs,
                                timeout_seconds=remaining_secs,
                            )
                        else:
                            from app.alert_dispatch import (
                                _maybe_create_local_runner_hold_alert,
                            )

                            created = await _maybe_create_local_runner_hold_alert(
                                task,
                                session,
                                held_seconds=held_secs,
                                timeout_seconds=remaining_secs,
                            )
                        if created:
                            await session.commit()
                    except Exception:
                        logger.exception(
                            "[RUNNERS] Failed to create early hold alert for task %s",
                            task.id,
                        )
                        await session.rollback()
            continue  # within the wait window — keep holding

        if is_strategic_hold:
            # CLOUD FALLBACK (self-heal): drop the hard-local pin and hand the
            # task back to normal dispatch so it provisions in the cloud. Once
            # STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY is cleared,
            # _task_requires_strategic_local_runner() is False, so the dispatch
            # loop no longer re-holds it. Stays QUEUED — not FAILED.
            minutes = max(1, strategic_timeout // 60)
            reason = (
                f"strategic_local_runner_required: no Strategic-capable local "
                f"runner became available within {minutes}m — falling back to "
                f"cloud dispatch so the project keeps making progress"
            )
            new_ctx = dict(ctx)
            new_ctx.pop(STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY, None)
            new_ctx.pop(STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY, None)
            new_ctx.pop(STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY, None)
            new_ctx.pop("preferred_runner_target_id", None)
            new_ctx.pop("runner_target_id", None)
            task.context = new_ctx
            task.status = TaskStatus.QUEUED  # explicit: re-enter normal dispatch
            task.vm_instance_id = None
            task.vm_provisioned_at = None
            task.latest_status_message = reason[:500]
            session.add(task)
            try:
                await _create_strategic_local_cloud_fallback_alert(
                    task, session, reason[:200]
                )
            except Exception:
                logger.exception(
                    "[RUNNERS] Failed to create Strategic cloud-fallback alert "
                    "for task %s",
                    task.id,
                )
            logger.info(
                "[RUNNERS] Strategic local-runner hold expired for task %s "
                "(type=%s): no live runner within %sm — falling back to cloud "
                "(held since %s)",
                task.id,
                task_type,
                minutes,
                stamped,
            )
            redispatched += 1
            continue

        # compute_experiment hold — terminal FAIL (no safe cloud fallback).
        # A strict runner_target pin gets a truthful, pin-specific reason
        # (runner id + why it never dispatched) instead of the generic
        # require-local wording, which would lie about why the task was held.
        hours = _LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS // 3600
        # Most-specific cause wins. A strict runner_target pin gets a truthful,
        # pin-specific reason (runner id + why it never dispatched) even when the
        # job also declares a container image — the pin diagnosis already names
        # the concrete blocker (e.g. that runner has no docker), which is strictly
        # more actionable than the generic container-capability wording.
        pin_hold = ctx.get("runner_pin_hold")
        if isinstance(pin_hold, dict) and pin_hold.get("runner_id"):
            reason = (
                f"runner_target: strict-pinned runner {pin_hold.get('runner_id')} "
                f"did not become eligible within {hours}h "
                f"(last: {pin_hold.get('reason_code') or 'unknown'}: "
                f"{pin_hold.get('detail') or 'no detail'}) — bring that runner "
                f"online / free its capacity, or relaunch without the strict "
                f"pin, then requeue"
            )
        elif is_container_hold:
            reason = (
                f"container_image: no container-capable runner (docker_usable) "
                f"became available within {hours}h — enroll a Docker-capable "
                f"runner and requeue, or requeue without a container_image"
            )
        else:
            reason = (
                f"require_local_runner: no self-hosted runner became available "
                f"within {hours}h — operator hardware offline; bring a runner "
                f"online and requeue"
            )
        task.status = TaskStatus.FAILED
        task.latest_status_message = reason[:500]
        persist_failure_category(task, PROVISION_FAILURE)
        session.add(task)
        try:
            await _maybe_create_failure_alert(task, session, reason[:200])
        except Exception:
            logger.exception(
                "[RUNNERS] Failed to create hold-timeout alert for task %s", task.id
            )
        record_task_result(
            success=False,
            failure_class=PROVISION_FAILURE,
            task_id=str(task.id),
            task_type=task_type or "unknown",
        )
        logger.warning(
            "[RUNNERS] Failed local-runner-held task %s (type=%s): no runner "
            "within %sh (held since %s)",
            task.id,
            task_type,
            hours,
            stamped,
        )
        failed += 1

    if failed or redispatched:
        await session.commit()
    return failed


async def _emit_all_accounts_blocked_stall(
    session: AsyncSession,
    earliest_recovery: datetime | None,
    blockage_reasons: set[str] | None = None,
) -> None:
    """Broadcast, alert, and notify operators that the pipeline is stalled on
    Max-plan account exhaustion. Intended to be called once per stall episode."""
    from app.broadcast import broadcast_to_project, manager as ws_manager
    from app.models import Alert, AlertSeverity
    from app.notification_dispatch import dispatch_external_notification

    recovery_iso = earliest_recovery.isoformat() if earliest_recovery else None
    title, msg = _format_stall_alert(blockage_reasons, earliest_recovery)
    try:
        await ws_manager.broadcast_to_all(
            "pipeline_throttle",
            {
                "reason": "all_accounts_blocked",
                "detail": msg,
                "earliest_recovery": recovery_iso,
                "blockage_reasons": sorted(blockage_reasons)
                if blockage_reasons
                else [],
            },
        )
    except Exception:
        logger.exception(
            "[ORCHESTRATOR] Failed to broadcast all_accounts_blocked throttle"
        )
    try:
        _stall_batch = 0
        _stall_batch_size = 500
        while True:
            project_ids = list(
                (
                    await session.execute(
                        select(Project.id)
                        .order_by(Project.id)
                        .offset(_stall_batch * _stall_batch_size)
                        .limit(_stall_batch_size)
                    )
                )
                .scalars()
                .all()
            )
            if not project_ids:
                break
            # Dedup across the "Pipeline stalled — ..." family, not the exact
            # title: with dynamic copy a new stall episode could otherwise stack
            # a second alert if its blockage shape (rate-limit vs capacity) flips.
            existing_rows = (
                await session.execute(
                    select(Alert.project_id).where(
                        Alert.project_id.in_(project_ids),
                        Alert.task_id.is_(None),
                        Alert.severity == AlertSeverity.ERROR,
                        Alert.dismissed_at.is_(None),
                        Alert.title.like(f"{_STALL_TITLE_PREFIX}%"),
                    )
                )
            ).all()
            projects_with_alert = {row[0] for row in existing_rows}
            for proj_id in project_ids:
                if proj_id not in projects_with_alert:
                    alert = Alert(
                        project_id=proj_id,
                        severity=AlertSeverity.ERROR,
                        title=title,
                        message=msg,
                    )
                    session.add(alert)
                    await broadcast_to_project(
                        str(proj_id),
                        "alert_created",
                        {
                            "alert_id": str(alert.id),
                            "severity": "ERROR",
                            "title": title,
                        },
                    )
                asyncio.ensure_future(
                    dispatch_external_notification(
                        proj_id,
                        "PIPELINE_STALLED",
                        {
                            "reason": "all_accounts_blocked",
                            "earliest_recovery": recovery_iso or "",
                            "detail": msg,
                        },
                    )
                )
            _stall_batch += 1
    except Exception:
        logger.exception(
            "[ORCHESTRATOR] Failed to create all-accounts-blocked stall alert"
        )
    logger.warning(f"[ORCHESTRATOR] {title}: {msg}")
