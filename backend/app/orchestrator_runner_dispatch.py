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


def _runner_active_task_count(runner: LocalRunner | RunnerDispatchSnapshot) -> int:
    """Best-effort current load, from the heartbeat-reported active task ids."""
    active = (runner.capabilities or {}).get("active_task_ids") or []
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


def _runner_at_capacity(
    runner: LocalRunner | RunnerDispatchSnapshot, pins_this_cycle: int = 0
) -> bool:
    """True when the runner's EFFECTIVE load has reached its declared max_concurrent.

    Effective load = heartbeat-reported active count (tasks the runner has already
    pulled and reported) PLUS ``pins_this_cycle`` (tasks pinned to this runner
    earlier in the SAME dispatch pass but not yet pulled/reported). The
    heartbeat-reported ``active_task_ids`` lags pins by at least one heartbeat, so
    without counting same-cycle pins a burst of N QUEUED tasks would all pin to a
    single idle runner (it reads as load 0 on every iteration) and the surplus —
    beyond what the runner can actually pull (max_concurrent) — would sit
    PROVISIONING until falsely reaped. Counting the in-cycle pins caps NEW work
    per cycle at the runner's real pull capacity.
    """
    caps = runner.capabilities or {}
    raw_max_concurrent = caps.get("max_concurrent")
    if isinstance(raw_max_concurrent, bool):
        max_concurrent = None
    elif isinstance(raw_max_concurrent, float):
        max_concurrent = (
            int(raw_max_concurrent) if raw_max_concurrent.is_integer() else None
        )
    else:
        try:
            max_concurrent = int(raw_max_concurrent)
        except (TypeError, ValueError):
            max_concurrent = None
    if max_concurrent is None:
        return False
    return _runner_active_work_count(runner) + pins_this_cycle >= max_concurrent


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
    outstanding_compute_by_runner = await _load_runner_compute_job_assignments(
        session, [runner_id for runner_id, _project_id, _capabilities, _failures, _hostname in rows]
    )
    by_project: dict[uuid.UUID, list[RunnerDispatchSnapshot]] = {}
    for runner_id, project_id, capabilities, consecutive_job_failures, hostname in rows:
        caps = dict(capabilities or {})
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
    runner = _select_eligible_runner(
        active_runners_cache,
        task.project_id,
        task_type,
        accelerator,
        runner_pins_this_cycle,
        preferred_runner_target_id=ctx.get("preferred_runner_target_id"),
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
    new_ctx.pop(STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY, None)
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
        from app.alert_dispatch import _resolve_local_runner_hold_alert

        await _resolve_local_runner_hold_alert(session, task.id)
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


def _select_eligible_runner(
    active_runners_cache: dict[uuid.UUID, list[LocalRunner | RunnerDispatchSnapshot]],
    project_id: uuid.UUID,
    task_type: str,
    accelerator: str | None,
    runner_pins_this_cycle: dict[uuid.UUID, int],
    preferred_runner_target_id: str | None = None,
) -> "LocalRunner | RunnerDispatchSnapshot | None":
    """Pick the least-loaded eligible runner for a task/job, or None.

    Eligibility: the runner handles ``task_type`` (its advertised allowlist) and is
    not at its per-cycle pull capacity; when an ``accelerator`` is declared it must
    also advertise ``has_gpu`` (a CPU runner cannot provide a GPU). A preferred
    runner wins only after those eligibility checks pass; otherwise selection
    prefers the least EFFECTIVE load — heartbeat-reported active count plus pins
    already made to the runner this cycle — so a same-cycle burst spreads across
    a fleet instead of stacking onto one runner.

    Shared by agent-task dispatch (:func:`_try_runner_dispatch`) and direct
    compute-job dispatch (:func:`_dispatch_compute_experiment_directly`) so both use
    a single eligibility / least-loaded rule.
    """
    candidates = active_runners_cache.get(project_id) or []
    eligible = [
        r
        for r in candidates
        if _runner_handles_task_type(r, task_type)
        and (task_type == "compute_experiment" or _runner_agent_available(r))
        and not _runner_at_capacity(r, runner_pins_this_cycle.get(r.id, 0))
    ]
    if accelerator:
        eligible = [r for r in eligible if (r.capabilities or {}).get("has_gpu")]
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
    def _dispatch_sort_key(r: "LocalRunner | RunnerDispatchSnapshot") -> tuple:
        failures = getattr(r, "consecutive_job_failures", 0) or 0
        is_degraded = int(failures >= _RUNNER_DEGRADED_FAILURE_THRESHOLD)
        return (is_degraded, _runner_active_work_count(r) + runner_pins_this_cycle.get(r.id, 0))

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
        ``"held"``     — ``require_local_runner`` set and no live runner; held QUEUED
                         (``awaiting_local_runner_since`` stamped; the existing bounded
                         sweep terminal-fails a hold that outlives the wait window).
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

    # ── Select an eligible runner (shared rule with _try_runner_dispatch) ──────
    runner = _select_eligible_runner(
        active_runners_cache,
        task.project_id,
        "compute_experiment",
        accelerator,
        runner_pins_this_cycle,
        preferred_runner_target_id=(task.context or {}).get(
            "preferred_runner_target_id"
        ),
    )

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

    # ── Pin to the chosen runner; the job inherits it via task.context ─────────
    if runner is not None:
        ctx = dict(task.context or {})
        ctx["runner_target_id"] = str(runner.id)
        ctx["runner_hostname"] = runner.hostname
        ctx.pop("awaiting_local_runner_since", None)
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
            await session.commit()
            try:
                await broadcast_to_project(
                    str(task.project_id),
                    "task_updated",
                    {"task_id": str(task.id), "status": TaskStatus.BLOCKED},
                )
            except Exception:
                logger.exception(
                    "[ORCHESTRATOR] task_updated broadcast failed for blocked "
                    "compute_experiment %s",
                    task.id,
                )
            logger.info(
                "[ORCHESTRATOR] compute_experiment %s BLOCKED on live-release "
                "precondition (recoverable): %s",
                task.id,
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
        from app.alert_dispatch import _resolve_local_runner_hold_alert

        await _resolve_local_runner_hold_alert(session, task.id)
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

    * **compute_experiment** (``context['awaiting_local_runner_since']``): held
      QUEUED instead of cloud-provisioned when no live self-hosted runner claimed
      it. A hold older than ``_LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS`` (6h) is marked
      terminal-FAILED (PROVISION_FAILURE) — a require-local compute run has no
      safe cloud fallback (it needs the operator's GPU), so failing is correct.

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
        strategic_stamp = ctx.get(STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY)
        is_compute_hold = (
            bool(compute_stamp)
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
        if not is_compute_hold and not is_strategic_hold:
            continue

        stamped = strategic_stamp if is_strategic_hold else compute_stamp
        stamped_at = _parse_local_runner_hold_timestamp(stamped)
        if stamped_at is None:
            continue
        cutoff = strategic_cutoff if is_strategic_hold else compute_cutoff
        if stamped_at > cutoff:
            # Within the full wait window — keep holding. For compute holds, emit a
            # proactive early-warning alert once the hold passes the shorter threshold
            # so the operator learns the hardware is offline in minutes, not 6h.
            if is_compute_hold:
                alert_cutoff = now - timedelta(seconds=_LOCAL_RUNNER_HOLD_ALERT_SECONDS)
                if stamped_at <= alert_cutoff:
                    from app.alert_dispatch import _maybe_create_local_runner_hold_alert

                    held_secs = int((now - stamped_at).total_seconds())
                    remaining_secs = max(
                        0, _LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS - held_secs
                    )
                    try:
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
        hours = _LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS // 3600
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
