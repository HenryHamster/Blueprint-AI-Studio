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

This module is itself a chronic size-limit offender (six recorded cap bumps),
and ``app.size_limit_policy`` forbids bumping it further, so its cohesive seams
now live in sibling modules and are re-exported below — a facade, exactly like
``app.orchestrator``'s. What remains here is the ORCHESTRATION core: the
runner-liveness / binding / load caches, ``_try_runner_dispatch``, the
strategic-local-runner path, stale-runner deactivation, the bounded hold sweep,
and the all-accounts-blocked stall alert.

  * :mod:`app.runner_eligibility` — per-runner predicates ("what does this
    runner advertise?").
  * :mod:`app.runner_capability_contracts` — per-task contracts ("what does
    this task require?"), the payload coercions, and the container-image
    free-disk floors.
  * :mod:`app.orchestrator_runner_selection` — ``_select_eligible_runner`` and
    the strict-pin hold diagnosis that must share its eligibility rule.
  * :mod:`app.orchestrator_runner_placement` — the durable ``local_only``
    execution-placement wait record.
  * :mod:`app.orchestrator_compute_experiment_dispatch` — direct
    compute-experiment launch, its hold markers, and its cap deferral.

The split is behaviour-preserving: every symbol moved verbatim and is
re-exported below, so both ``from app.orchestrator_runner_dispatch import X``
and monkeypatch targets naming this module keep resolving to the same object.
Add new code to the sibling that owns the seam, not here — the tripwire in
``testing/test_orchestrator_modularity.py`` fails on a shadowing redefinition.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.deploy_identity import deploy_repo_identity, project_matches_deploy_identity
from app.execution_capability_requirements import (
    CAPABILITY_BACKEND_VERIFICATION,
    EXECUTION_CAPABILITY_WAIT_CONTEXT_KEY,
    has_learned_execution_capability,
    learned_execution_capabilities,
)
from app.models import (
    ComputeJob,
    ComputeJobStatus,
    LocalRunner,
    Project,
    RunnerProjectBinding,
    Task,
    TaskStatus,
)
from app.resource_envelope import resolve_project_execution_placement_policy
from app.runner_pins import release_unpulled_runner_pins
from app.runner_decline import (
    decline_exclusion_is_advisory,
    recently_declined_runner_ids,
)
from app.runner_health_monitors import (  # noqa: F401
    _RUNNER_DEGRADED_FAILURE_THRESHOLD,
    _RUNNER_LIVENESS_SECONDS,
    _RUNNER_LOW_DISK_THRESHOLD_BYTES_DEFAULT,
    _RUNNER_LOW_MEMORY_THRESHOLD_BYTES_DEFAULT,
    _VM_LOW_DISK_THRESHOLD_BYTES_DEFAULT,
    _VM_LOW_MEMORY_THRESHOLD_BYTES_DEFAULT,
    _check_degraded_runners,
    _check_fleet_no_healthy_runner,
    _check_idle_gpu_runners,
    _check_low_disk_runners,
    _check_low_disk_vms,
    _check_low_memory_runners,
    _check_low_memory_vms,
    _sweep_runner_deactivated_escalations,
)
from app.shared_state import runtime_config as _runtime_config
from app.stall_alert_diagnostics import (
    _STALL_TITLE_PREFIX,
    _format_stall_alert,
)
from app.task_capabilities import (
    STRATEGIC_TASK_TYPES,
    execution_site_is_generic_dispatchable,
)
from app.time_utils import _utcnow
from app.workbench_events import (
    SURFACE_RUNTIME,
    SURFACE_STRATEGIC_SESSION,
    emit_workbench_refetch,
)
from app.strategic_runtime_work import (
    CONTAINER_RUNNER_HOLD_CONTEXT_KEY,
    LOCAL_RUNNER_HOLD_CONTEXT_KEY,
)
from app.workbench_state import (
    STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY,
)

logger = logging.getLogger("blueprint")


# ── Runner selection + strict-pin diagnosis ──────────────────────────────────
# Moved to app/orchestrator_runner_selection.py. _select_eligible_runner and
# _diagnose_strict_runner_pin_hold must share one eligibility rule, so they were
# extracted together. Re-exported so every existing import path keeps working.
from app.orchestrator_runner_selection import (  # noqa: F401
    FORCED_RUNNER_TARGET_CONTEXT_KEY,
    _diagnose_strict_runner_pin_hold,
    _lookup_runner_hostname,
    _num_or_none,
    _payload_runner_target,
    _PIN_HOLD_DIGITS_RE,
    _pin_hold_stable_key,
    _select_eligible_runner,
)

# ── Execution-placement waits ────────────────────────────────────────────────
# Moved to app/orchestrator_runner_placement.py (chronic size-limit offender;
# decomposition required, cap bumps forbidden by app.size_limit_policy).
# Re-exported so `orch._hold_task_for_local_only_placement` and every existing
# import path keeps working unchanged.
from app.orchestrator_runner_placement import (  # noqa: F401
    EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY,
    _clear_execution_placement_wait,
    _hold_task_for_local_only_placement,
    _stage_local_only_placement_wait,
)

# ── Compute-experiment dispatch ──────────────────────────────────────────────
# Moved to app/orchestrator_compute_experiment_dispatch.py. The hold-marker
# constants are STAMPED there and SWEPT here by _fail_stale_local_runner_holds,
# so they are imported back rather than re-declared: a second copy of
# "awaiting_forced_runner_since" is the exact drift the aggregate prevents.
from app.orchestrator_compute_experiment_dispatch import (  # noqa: F401
    AWAITING_FORCED_RUNNER_CONTEXT_KEY,
    COMPUTE_LAUNCH_DEFERRED_REASON_CONTEXT_KEY,
    COMPUTE_LAUNCH_DEFER_COUNT_CONTEXT_KEY,
    COMPUTE_RUNNER_HOLD_CONTEXT_KEYS,
    _COMPUTE_CAP_DEFER_ALERT_AFTER_DEFERS,
    _COMPUTE_CAP_DEFER_ALERT_TITLE,
    _COMPUTE_CAP_DEFER_BASE_MINUTES,
    _COMPUTE_CAP_DEFER_MAX_MINUTES,
    _build_compute_launch_kwargs,
    _defer_compute_experiment_task,
    _dispatch_compute_experiment_directly,
    _fail_compute_experiment_task,
    _maybe_alert_saturated_compute_cap_defer,
)


# A compute_experiment held for a require-local project is failed if no runner
# claims it within this window — prevents a permanently-wedged project when the
# operator's hardware stays offline. Generous default; operators can bring a
# runner up well within it.
_LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS = 6 * 3600

# Early-warning threshold: emit a proactive WARNING alert when a require-local
# compute_experiment hold has waited past this window but before the 6h hard
# timeout — so the operator learns the hardware is offline in minutes, not hours.
_LOCAL_RUNNER_HOLD_ALERT_SECONDS = 15 * 60  # 15 minutes


# ── Task-side capability contracts ───────────────────────────────────────────
# Moved to app/runner_capability_contracts.py: the payload coercions, the
# container-image free-disk floors, and the browser / project-verification
# contracts that join a task's demands to a runner's advertised capabilities.
# Re-exported so app.routers.runner and every existing import path keep working.
from app.runner_capability_contracts import (  # noqa: F401
    _BROWSER_EVIDENCE_KINDS,
    _BROWSER_RUNTIME_PATHS,
    _BROWSER_RUNTIME_PREFIXES,
    _CONTAINER_IMAGE_MIN_FREE_DISK_BYTES_DEFAULT,
    _CONTAINER_WORKSPACE_MIN_FREE_DISK_BYTES_DEFAULT,
    _coerce_min_compute_capability,
    _coerce_vram_gb,
    _container_image_min_free_disk_bytes,
    _container_workspace_min_free_disk_bytes,
    _fleet_missing_capability_reason,
    _paths_require_browser,
    _payload_requires_browser,
    _runner_lacks_required_browser,
    _runner_lacks_required_project_verification,
    _task_requires_browser,
    _task_requires_project_verification_pinned,
)


# ── Runner eligibility predicates ────────────────────────────────────────────
# Moved to app/runner_eligibility.py. This module is a chronic size-limit
# offender (decomposition required; cap bumps are forbidden by
# app.size_limit_policy), and the capability predicates were the one large,
# perfectly self-contained unit here: pure functions over a runner snapshot, no
# session, no I/O. Re-exported so every existing `orch._runner_*` reference and
# import path keeps working unchanged.
from app.runner_eligibility import (  # noqa: F401
    _ACTIVE_AGENT_TASK_COUNT_KEY,
    RunnerDispatchSnapshot,
    _coerce_max_concurrent,
    _job_requires_gpu_in_container,
    _runner_active_compute_job_count,
    _runner_active_task_count,
    _runner_active_work_count,
    _runner_accepting_new_work,
    _runner_agent_available,
    _runner_at_capacity,
    _runner_at_compute_capacity,
    _runner_chromium_usable,
    _runner_container_image_present,
    _runner_container_image_storage,
    _runner_effective_workspace_disk_floor,
    _runner_gpu_capable,
    _runner_gpu_in_container_capable,
    _runner_gpu_passthrough_blocker,
    _runner_handles_task_type,
    _runner_has_container_capability,
    _runner_has_free_disk,
    _runner_has_free_vram,
    _runner_has_project_verification,
    _runner_max_concurrent_compute,
    _runner_meets_min_compute_capability,
    _runner_python_runtime_incapable,
    _task_is_authoring,
    _task_requires_project_verification,
)



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
                    LocalRunner.source_sha,
                    LocalRunner.agent_source_sha,
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
        row[0] for row in rows
    ]
    outstanding_compute_by_runner = await _load_runner_compute_job_assignments(
        session, runner_ids_for_load
    )
    # Per-runner project allow-list bindings (many-to-many). A runner is a dispatch
    # candidate under EVERY project on its allow-list, not only its enrollment
    # project, so one daemon serves N projects. The enrollment project is the
    # implicit anchor member and is folded in below regardless of binding rows.
    bound_projects_by_runner = await _load_runner_project_bindings(
        session, runner_ids_for_load
    )
    # Authoritative per-runner agent-task load from Task rows (heartbeat-independent).
    active_task_count_by_runner = await _load_runner_active_task_counts(
        session, runner_ids_for_load
    )
    by_project: dict[uuid.UUID, list[RunnerDispatchSnapshot]] = {}
    for (
        runner_id,
        project_id,
        capabilities,
        consecutive_job_failures,
        hostname,
        source_sha,
        agent_source_sha,
    ) in rows:
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
            source_sha=source_sha,
            agent_source_sha=agent_source_sha,
        )
        # Register the SAME snapshot object under every project on the runner's
        # allow-list (enrollment project UNIONed with its explicit bindings). Sharing
        # one object per physical box is what keeps the GPU/concurrency ledger
        # unified: per-cycle pins and heartbeat-reported active counts are read off
        # this single snapshot, so a project-B pin still counts against the same
        # box's capacity when a project-A task is next considered — one physical GPU
        # can never be double-booked across two projects.
        allowed_project_ids = {project_id} | bound_projects_by_runner.get(
            runner_id, set()
        )
        for allowed_project_id in allowed_project_ids:
            by_project.setdefault(allowed_project_id, []).append(snapshot)
    return by_project


async def _load_runner_project_bindings(
    session: AsyncSession,
    runner_ids: list[uuid.UUID],
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """Return the EXPLICIT project allow-list bindings keyed by runner id.

    Reads ``runner_project_binding`` rows for the given runner ids so the dispatch
    cache can register each runner as a candidate under every project on its
    allow-list. The enrollment project (``LocalRunner.project_id``) is the implicit
    anchor member and is folded in by the caller, so this returns only the EXPLICIT
    bindings. Best-effort: on a query error returns an empty dict, degrading to
    enrollment-project-only grouping rather than failing the whole dispatch cycle.
    """
    if not runner_ids:
        return {}
    try:
        rows = (
            await session.execute(
                select(
                    RunnerProjectBinding.runner_id,
                    RunnerProjectBinding.project_id,
                ).where(RunnerProjectBinding.runner_id.in_(runner_ids))
            )
        ).all()
    except Exception:
        logger.exception("[RUNNERS] Failed to load runner project bindings")
        return {}
    by_runner: dict[uuid.UUID, set[uuid.UUID]] = {}
    for runner_id, project_id in rows:
        by_runner.setdefault(runner_id, set()).add(project_id)
    return by_runner


async def _load_runner_compute_job_assignments(
    session: AsyncSession,
    runner_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[str]]:
    """Return outstanding runner-subprocess compute jobs keyed by runner id.

    Heartbeat-reported compute ids lag direct ``compute_experiment`` launch by at
    least one runner poll/heartbeat. Counting persisted runner-subprocess jobs
    closes that window so a later orchestrator cycle cannot over-assign a runner
    before it has claimed the first job.

    Attribution is by committed DB fact, not the lossy heartbeat list:

    - A CLAIMED, in-flight job (status RUNNING/POLLING) is attributed to
      ``ran_on_runner_id`` — the runner that actually holds it, stamped at
      ``/next-compute-job`` claim time. For robustness on legacy rows predating
      that column, a RUNNING/POLLING job with a NULL ``ran_on_runner_id`` falls
      back to the ``polling_metadata['runner_target_id']`` dispatch pin.
    - A NOT-YET-CLAIMED job (status QUEUED/STARTING, ``ran_on_runner_id`` still
      NULL) is attributed to the ``runner_target_id`` reservation pin so a
      reserved-but-unclaimed slot is still counted against its target runner.
    """
    if not runner_ids:
        return {}
    from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME
    from sqlalchemy import or_

    runner_id_strings = [str(runner_id) for runner_id in runner_ids]
    runner_target_expr = ComputeJob.polling_metadata["runner_target_id"].as_string()
    claimed_statuses = {ComputeJobStatus.RUNNING, ComputeJobStatus.POLLING}
    try:
        rows = (
            await session.execute(
                select(
                    ComputeJob.id,
                    ComputeJob.status,
                    ComputeJob.ran_on_runner_id,
                    runner_target_expr,
                )
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
                    or_(
                        ComputeJob.ran_on_runner_id.in_(runner_ids),
                        runner_target_expr.in_(runner_id_strings),
                    ),
                )
                .order_by(ComputeJob.created_at.asc())
            )
        ).all()
    except Exception:
        logger.exception("[RUNNERS] Failed to load runner compute-job assignments")
        return {}

    by_runner: dict[uuid.UUID, list[str]] = {}
    runner_ids_set = set(runner_ids)
    runner_ids_by_string = {
        str(runner_id): runner_id
        for runner_id in runner_ids
    }
    for job_id, status, ran_on_runner_id, raw_runner_id in rows:
        # A claimed in-flight job belongs to the runner that actually holds it
        # (ran_on_runner_id, stamped at claim). Fall back to the reservation pin
        # only when that authoritative record is absent — i.e. legacy rows
        # predating the column, or jobs not yet claimed (QUEUED/STARTING, where
        # ran_on_runner_id is still NULL and the pin reserves the slot).
        if status in claimed_statuses and ran_on_runner_id is not None:
            attributed_runner = (
                ran_on_runner_id if ran_on_runner_id in runner_ids_set else None
            )
        else:
            attributed_runner = runner_ids_by_string.get(str(raw_runner_id))
        if attributed_runner is None:
            continue
        by_runner.setdefault(attributed_runner, []).append(str(job_id))
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
    # A task whose registry execution site is not the generic default is a hard
    # routing contract generic dispatch cannot satisfy: the qualifier's
    # ``walkthrough_e2e`` runs only on a live runner proven for the candidate-stack
    # site (docker info + Compose + Chromium) and is placed at birth by the
    # qualifier, while the direct-GCE site is provisioned cold.  Do not let a live
    # runner claim such a task merely because it advertises a generic
    # agent/browser capability; the registry site contract is the dispatch
    # authority and must not fall back to the default matrix.
    if not execution_site_is_generic_dispatchable(task_type):
        return False
    project = (
        await session.get(Project, task.project_id)
        if _task_is_authoring(task_type)
        else None
    )
    # The generated publication gate treats this deployment's own repo as a
    # special fail-closed case: any authoring task may touch backend/ and then
    # must run its Python/Postgres checks.  Mirror that requirement at dispatch
    # even if the optional per-directory declaration drifted out of build_config;
    # external projects retain the declaration-driven contract unchanged.
    deploy_identity = deploy_repo_identity()
    is_deploy_repo = bool(
        project is not None
        and deploy_identity is not None
        and project_matches_deploy_identity(project, deploy_identity)
    )
    if project is not None and deploy_identity is None:
        # Dispatch uses a deliberate raiseload projection that excludes the
        # legacy is_self_repo field. Query that one scalar only on unstamped
        # deployments; synchronously reading the ORM attribute would crash the
        # entire dispatch cycle, while dropping the fallback would fail open.
        is_deploy_repo = bool(
            (
                await session.execute(
                    select(Project.is_self_repo).where(Project.id == project.id)
                )
            ).scalar_one_or_none()
        )
    requires_project_verification = bool(
        has_learned_execution_capability(
            task.context, CAPABILITY_BACKEND_VERIFICATION
        )
        or (
            project is not None
            and _task_is_authoring(task_type)
            and (
                is_deploy_repo
                or _task_requires_project_verification(task_type, project.build_config)
            )
        )
    )
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
    # Coerced defensively: this is agent-authored payload data, and a raw float()
    # here would raise INSIDE the dispatch cycle on e.g. "24GB". Decline to pin
    # (return False) rather than crash the cycle — the task falls through to the
    # compute-experiment dispatch path, which fails IT with an honest validation
    # message naming the bad field (see _dispatch_compute_experiment_task).
    vram_gb, vram_error = _coerce_vram_gb(payload)
    if vram_error is not None:
        logger.warning(
            "[RUNNERS] Not pinning task %s to a runner: invalid vram_gb — %s",
            task.id,
            vram_error,
        )
        return False
    # A declared min_compute_capability (image GPU-arch floor) restricts eligibility
    # to runners whose GPUs meet it. Coerced defensively like vram_gb — a bad value
    # declines to pin (falls through to the compute path, which fails IT honestly).
    min_compute_capability, min_cc_error = _coerce_min_compute_capability(payload)
    if min_cc_error is not None:
        logger.warning(
            "[RUNNERS] Not pinning task %s to a runner: invalid "
            "min_compute_capability — %s",
            task.id,
            min_cc_error,
        )
        return False
    requires_browser = _task_requires_browser(task)
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
        min_compute_capability=min_compute_capability,
        # Runners that recently declined THIS task are excluded — otherwise the
        # per-task-stable tiebreak re-pins it to the same runner that just said
        # no, and the pin→pull→decline loop never terminates.
        declined_runner_ids=recently_declined_runner_ids(task),
        # ...but never at the cost of a local-required Strategic task's local
        # execution: on a single-runner Strategic fleet an exclusion empties the
        # pool, and the 900s hold sweep then evicts the task to cloud. While the
        # declines still look transient, re-pick the decliner instead.
        declined_exclusion_advisory=decline_exclusion_is_advisory(task, task_type),
        requires_project_verification=requires_project_verification,
        requires_browser=requires_browser,
    )
    if runner is None:
        # Zero-fleet-wide-capability case: surface WHICH capability is missing
        # rather than a generic "waiting for capacity" reason. This mutation is
        # in-memory only (no commit) — the immediately-following
        # ``_hold_task_for_local_only_placement`` call reads the same ORM
        # object in the same transaction and folds it into the held task's
        # durable reason/message; any other path (cloud fallback, a future
        # cycle) simply leaves it stale until overwritten or the task pins.
        capability_reason = _fleet_missing_capability_reason(
            active_runners_cache.get(task.project_id) or [],
            requires_project_verification=requires_project_verification,
            requires_browser=requires_browser,
        )
        if capability_reason:
            ctx_with_reason = dict(ctx)
            ctx_with_reason["capability_dispatch_reason"] = capability_reason
            task.context = ctx_with_reason
        return False

    new_ctx = dict(ctx)
    new_ctx["runner_target_id"] = str(runner.id)
    new_ctx["runner_hostname"] = runner.hostname
    # Stamped so the claim-time backstop in POST /next-task
    # (_runner_lacks_required_project_verification) can read the requirement
    # back cheaply instead of re-querying the project per polled candidate.
    new_ctx["requires_project_verification"] = requires_project_verification
    # A task that was held (enforce-local) and is now pinned must not carry the
    # stale wait stamp — otherwise the bounded-timeout sweep could fail it after
    # it has already been claimed by a runner.
    new_ctx.pop(LOCAL_RUNNER_HOLD_CONTEXT_KEY, None)
    new_ctx.pop(CONTAINER_RUNNER_HOLD_CONTEXT_KEY, None)
    new_ctx.pop(STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY, None)
    new_ctx.pop(EXECUTION_CAPABILITY_WAIT_CONTEXT_KEY, None)
    new_ctx.pop("runner_pin_hold", None)
    new_ctx.pop("capability_dispatch_reason", None)
    _clear_execution_placement_wait(new_ctx)
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


async def _hold_task_for_learned_execution_capabilities(
    session: AsyncSession,
    task: Task,
) -> bool:
    """Hold a learned requirement instead of trying an unproved cloud site."""

    capabilities = learned_execution_capabilities(task.context)
    if not capabilities:
        return False
    ctx = dict(task.context or {})
    prior_wait = ctx.get(EXECUTION_CAPABILITY_WAIT_CONTEXT_KEY)
    since = (
        prior_wait.get("since")
        if isinstance(prior_wait, dict) and isinstance(prior_wait.get("since"), str)
        else _utcnow().isoformat()
    )
    wait_record = {
        "capabilities": sorted(capabilities),
        "since": since,
    }
    wait_message = (
        "Waiting for a live runner with learned execution capabilities: "
        + ", ".join(sorted(capabilities))
    )[:500]
    if (
        prior_wait == wait_record
        and task.vm_instance_id is None
        and task.vm_provisioned_at is None
        and task.latest_status_message == wait_message
        and not ctx.get("runner_target_id")
        and not ctx.get("runner_hostname")
    ):
        # The orchestrator selected this task FOR UPDATE before reaching this
        # helper.  An idempotent hold has nothing dirty for its outer commit
        # gate, so explicitly end the transaction instead of retaining the row
        # lock through the rest of the orchestration cycle.
        await session.rollback()
        return True
    ctx[EXECUTION_CAPABILITY_WAIT_CONTEXT_KEY] = wait_record
    ctx.pop("runner_target_id", None)
    ctx.pop("runner_hostname", None)
    task.context = ctx
    task.vm_instance_id = None
    task.vm_provisioned_at = None
    task.latest_status_message = wait_message
    session.add(task)
    await session.commit()
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






async def _deactivate_stale_runners(session: AsyncSession) -> None:
    """Mark runners that missed the heartbeat window as inactive.

    Keeps the dispatch cache query clean and surfaces stale runners in the admin
    listing. Ordinary tasks pinned to a now-inactive runner that the runner
    never pulled (``started_at IS NULL``) are released back to QUEUED so the
    next dispatch cycle can re-pin them. The exact canonical active Resident is
    preserved for session lifecycle recovery instead of silently migrating it.
    This recovery is required because the orphaned-task reaper intentionally no
    longer reaps awaiting-pull runner pins (they are not dead GCE VMs).
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

    # Release ordinary pins while preserving only the exact canonical active
    # Resident for each automatic runner transition.  Stale Resident-shaped
    # markers are releasable; explicit admin pause/delete callers retain the
    # helper's default generic-release behavior.
    released_pins_by_project: dict[uuid.UUID, int] = {}
    for runner_id, project_id, *_rest in stale_rows:
        released = await release_unpulled_runner_pins(
            session,
            runner_id,
            preserve_active_resident=True,
        )
        if not released:
            continue
        released_pins_by_project[project_id] = (
            released_pins_by_project.get(project_id, 0) + released
        )
        logger.info(
            "[RUNNERS] Released %d unpulled pin(s) from now-inactive runner %s "
            "(back to QUEUED for re-dispatch)",
            released,
            runner_id,
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
        except Exception:  # noqa: PERF203
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
        event_type="runner.degraded",
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

    Legacy hold classes share a bounded-wait contract but resolve differently.
    The explicit project-level ``local_only`` placement wait is deliberately
    outside that contract: it remains QUEUED until local capacity returns.

    * **compute_experiment** (``context['awaiting_local_runner_since']``,
      ``context['awaiting_container_runner_since']`` or
      ``context['awaiting_forced_runner_since']``): held QUEUED instead of
      cloud-provisioned when no live self-hosted runner claimed it (require-local),
      no runner advertises ``docker_usable`` for an image-declaring job
      (container hold), or the operator's HARD ``forced_runner_target_id`` pin is
      not eligible (forced hold). A hold older than
      ``_LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS`` (6h) is marked terminal-FAILED
      (PROVISION_FAILURE) — no variant has a safe cloud fallback (each needs
      specific operator hardware/runtime/host), so failing is correct. All three
      emit an early-warning alert first.

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
    retained_for_local_only = 0
    for task in rows:
        ctx = task.context or {}
        task_type = str((task.first_payload or {}).get("type") or "")
        compute_stamp = ctx.get("awaiting_local_runner_since")
        container_stamp = ctx.get("awaiting_container_runner_since")
        forced_stamp = ctx.get(AWAITING_FORCED_RUNNER_CONTEXT_KEY)
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
        # Forced-runner hold: an operator HARD pin (forced_runner_target_id) whose
        # target runner is not eligible. Same bounded-wait contract as its siblings
        # above — it has no cloud fallback BY DESIGN (the whole point of the pin is
        # "this host or nothing"), which is exactly why it must be bounded: without
        # this branch the stamp was written and never read, so the task waited QUEUED
        # forever with no timeout and no alert. Lowest precedence: a coexisting
        # require-local / container stamp names a more specific blocker.
        is_forced_runner_hold = (
            bool(forced_stamp)
            and not is_compute_hold
            and not is_container_hold
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
        if (
            not is_compute_hold
            and not is_container_hold
            and not is_forced_runner_hold
            and not is_strategic_hold
        ):
            continue

        # A task may have acquired the explicit project-level policy after it
        # was parked under one of the legacy, bounded hold markers.  Migrate it
        # in-place before any timeout logic: local_only is durable backpressure
        # for every task type, so an unavailable runner must not trigger either
        # the old compute terminal failure or the old Strategic cloud self-heal.
        project = await session.get(Project, task.project_id)
        if (
            project is not None
            and resolve_project_execution_placement_policy(project) == "local_only"
        ):
            new_ctx = dict(ctx)
            for hold_key in COMPUTE_RUNNER_HOLD_CONTEXT_KEYS:
                new_ctx.pop(hold_key, None)
            new_ctx.pop(STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY, None)
            new_ctx.pop(STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY, None)
            new_ctx.pop(STRATEGIC_LOCAL_RUNNER_REASON_CONTEXT_KEY, None)
            task.context = new_ctx
            _stage_local_only_placement_wait(
                task,
                reason="no_eligible_local_runner",
            )
            session.add(task)
            retained_for_local_only += 1
            logger.info(
                "[RUNNERS] Retained task %s in local_only placement wait instead "
                "of resolving legacy runner hold (%s)",
                task.id,
                task_type,
            )
            continue

        if is_strategic_hold:
            stamped = strategic_stamp
        elif is_container_hold:
            stamped = container_stamp
        elif is_forced_runner_hold:
            stamped = forced_stamp
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
            if is_compute_hold or is_container_hold or is_forced_runner_hold:
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
        elif is_forced_runner_hold:
            # Name the host the operator actually pinned — "forced runner dgx-1"
            # is actionable; "no self-hosted runner" would be a lie (other runners
            # may well have been online and idle; the pin is what excluded them).
            forced_id = str(ctx.get(FORCED_RUNNER_TARGET_CONTEXT_KEY) or "").strip()
            forced_hostname = await _lookup_runner_hostname(
                session, task.project_id, forced_id
            )
            target_label = forced_hostname or forced_id or "unknown"
            reason = (
                f"forced_runner_target_id: hard-pinned runner {target_label} did "
                f"not become eligible within {hours}h — a forced pin never falls "
                f"back to another runner or to cloud. Bring that runner online / "
                f"free its capacity, or relaunch without the forced pin, then "
                f"requeue"
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

    if failed or redispatched or retained_for_local_only:
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
                        event_type="capacity.all_accounts_blocked",
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
                asyncio.ensure_future(  # noqa: RUF006
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
