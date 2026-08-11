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
import math
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.deploy_identity import deploy_repo_identity, project_matches_deploy_identity
from app.models import (
    ComputeJob,
    ComputeJobStatus,
    LocalRunner,
    Project,
    RunnerProjectBinding,
    Task,
    TaskStatus,
)
from app.resource_envelope import (
    resolve_project_execution_placement_policy,
    resolve_project_require_local_runner,
)
from app.runner_project_binding import allowed_project_ids
from app.runner_decline import (
    decline_exclusion_is_advisory,
    decline_record_for_runner,
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


# task.context key holding an operator's HARD runner pin for a compute_experiment.
# Kept deliberately SEPARATE from the soft ``preferred_runner_target_id`` affinity
# hint: a forced target NEVER silently falls back to a different runner or to
# cloud — when it is not eligible this cycle the task is HELD QUEUED. The soft
# preferred hint keeps its fallback behaviour untouched.
FORCED_RUNNER_TARGET_CONTEXT_KEY = "forced_runner_target_id"

# One durable, task-wide wait record for the project placement contract.  Unlike
# the older capability-specific timestamps below, this is not a failure timer:
# ``local_only`` explicitly means that lack of capacity is backpressure, not a
# reason to spend money elsewhere or to fail useful work.  Keep the value a
# structured record so operators can distinguish a policy wait from a runner
# capability fault without adding another top-level timestamp for each task type.
EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY = "execution_placement_wait"


def _stage_local_only_placement_wait(task: Task, *, reason: str) -> bool:
    """Stage a durable ``local_only`` wait and return whether it changed ``task``.

    This helper intentionally does not commit.  Dispatch callers can commit the
    wait immediately, while timeout/recovery code can stage it together with its
    other updates.  The original ``since`` is retained while the policy and
    reason are stable so queue age is truthful across dispatcher cycles.
    """
    ctx = dict(task.context or {})
    current = ctx.get(EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY)
    current_since = (
        current.get("since")
        if isinstance(current, dict) and isinstance(current.get("since"), str)
        else None
    )
    wait_record = {
        "policy": "local_only",
        "reason": reason,
        "since": current_since or _utcnow().isoformat(),
    }
    changed = ctx.get(EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY) != wait_record
    if ctx.pop("runner_target_id", None) is not None:
        changed = True
    if ctx.pop("runner_hostname", None) is not None:
        changed = True
    ctx[EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY] = wait_record
    message = "Waiting for local execution capacity (project policy: local_only)."
    if task.latest_status_message != message:
        task.latest_status_message = message
        changed = True
    if changed:
        task.context = ctx
    return changed


def _clear_execution_placement_wait(ctx: dict) -> None:
    """Clear a policy wait once a task has durably acquired local execution."""
    ctx.pop(EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY, None)


async def _hold_task_for_local_only_placement(
    session: AsyncSession,
    task: Task,
    *,
    reason: str = "no_eligible_local_runner",
) -> bool:
    """Keep a ``local_only`` task queued when no runner can take it.

    Returns ``True`` only when the project contract is strict and the task was
    handled.  It deliberately has no timeout: the policy promises local
    execution, so absence of a runner must never become an implicit managed
    fallback or a terminal failure.
    """
    project = await session.get(Project, task.project_id)
    if (
        project is None
        or resolve_project_execution_placement_policy(project) != "local_only"
    ):
        return False
    task_id = task.id
    changed = _stage_local_only_placement_wait(task, reason=reason)
    if changed:
        session.add(task)
        await session.commit()
    else:
        # Keep the orchestrator's transaction boundary small.  Do not touch
        # ``task`` after this rollback: SQLAlchemy expires ORM attributes.
        await session.rollback()
    logger.info(
        "[RUNNERS] Holding task %s for local_only placement: %s",
        task_id,
        reason,
    )
    return True

# task.context marker stamped (ISO-8601) the first cycle a forced-pin
# compute_experiment is held waiting for its target runner to become eligible —
# the honest, operator-legible "waiting for dgx-1" signal, mirroring the
# ``awaiting_local_runner_since`` / ``awaiting_container_runner_since`` holds.
AWAITING_FORCED_RUNNER_CONTEXT_KEY = "awaiting_forced_runner_since"

# Every task.context key that marks a compute_experiment HELD QUEUED at dispatch
# because no runner could take it this cycle. This is the set the dispatch path STAMPS,
# and the set it CLEARS on successful dispatch (the only place a stamp is cleared).
#
# COMPOSED from the canonical per-key constants rather than re-typing their string
# literals: a third independent copy of "awaiting_local_runner_since" et al. is the
# exact drift this aggregate exists to prevent.
#
# It IS in lockstep with the bounded hold-timeout sweep: every key below is both
# STAMPED here and SWEPT by ``_fail_stale_local_runner_holds`` (15-min early operator
# alert, then 6h terminal PROVISION_FAILURE), upholding the invariant:
#
#   a key that is stamped but never swept is an unbounded hold, and a key that dispatch
#   stops stamping but the sweep still reads is dead code.
#
# AWAITING_FORCED_RUNNER_CONTEXT_KEY was previously stamped but NEVER swept — a
# forced-pin hold whose target runner never came back waited QUEUED indefinitely with
# nothing watching it. The sweep now covers it on the same bounded-wait contract as its
# siblings, with a forced-pin-specific terminal reason naming the pinned runner.
COMPUTE_RUNNER_HOLD_CONTEXT_KEYS: tuple[str, ...] = (
    LOCAL_RUNNER_HOLD_CONTEXT_KEY,
    CONTAINER_RUNNER_HOLD_CONTEXT_KEY,
    AWAITING_FORCED_RUNNER_CONTEXT_KEY,
)

# task.context marker stamped when a compute_experiment launch is deferred by a
# TRANSIENT precondition (currently: the project's active-ComputeJob concurrency
# cap). Distinct from the hold markers above — nothing is "awaited" here, the task
# is simply re-queued behind a retry_after backoff and will launch unchanged once a
# running job finishes and frees cap. Carries the human-readable reason so the
# deferral is legible instead of looking like an idle QUEUED task.
COMPUTE_LAUNCH_DEFERRED_REASON_CONTEXT_KEY = "compute_launch_deferred_reason"

# Consecutive-deferral counter driving the backoff escalation below. Deliberately
# NOT the shared ``_retry_after_resets``: the candidate scan clears that counter the
# moment a backoff lapses, so escalation built on it would flatten to the base delay
# forever. This key is owned by the deferral path and cleared only once the task
# actually launches.
COMPUTE_LAUNCH_DEFER_COUNT_CONTEXT_KEY = "compute_launch_defer_count"

# Backoff bounds (minutes) for the transient-cap deferral above. Escalates per
# consecutive deferral so a project sitting at its cap for hours (long GPU jobs)
# re-checks cheaply instead of re-probing every dispatch cycle. Both are well inside
# set_retry_after's own [0, 30] clamp.
_COMPUTE_CAP_DEFER_BASE_MINUTES = 2.0
_COMPUTE_CAP_DEFER_MAX_MINUTES = 15.0

# Raise ONE operator alert once a task has deferred this many consecutive times. The
# backoff saturates at _COMPUTE_CAP_DEFER_MAX_MINUTES from the 3rd deferral, so by the
# 8th the task has been waiting on a full compute-job cap for well over an hour with no
# sign of it clearing. A deferral is correct and unbounded BY DESIGN (it self-clears
# whenever a running job goes terminal) — which is exactly why a saturated one needs a
# surface: nothing else will ever mention it.
_COMPUTE_CAP_DEFER_ALERT_AFTER_DEFERS = 8

_COMPUTE_CAP_DEFER_ALERT_TITLE = "Compute experiment stalled at project job cap"


# A compute_experiment held for a require-local project is failed if no runner
# claims it within this window — prevents a permanently-wedged project when the
# operator's hardware stays offline. Generous default; operators can bring a
# runner up well within it.
_LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS = 6 * 3600

# Early-warning threshold: emit a proactive WARNING alert when a require-local
# compute_experiment hold has waited past this window but before the 6h hard
# timeout — so the operator learns the hardware is offline in minutes, not hours.
_LOCAL_RUNNER_HOLD_ALERT_SECONDS = 15 * 60  # 15 minutes


def _coerce_vram_gb(payload: dict) -> tuple[float | None, str | None]:
    """Resolve the agent-authored ``vram_gb`` hint to a float, or an honest error.

    ``vram_gb`` is AGENT-AUTHORED free-form payload data read in the dispatch hot
    path, so it must be treated as untrusted. A bare ``float(raw)`` raises on the
    values agents actually write — ``"24GB"`` (unit suffix), ``[24]``, ``{}`` —
    and that exception propagates out of the dispatch cycle, taking down dispatch
    for EVERY task in the cycle (blast radius: the whole fleet) while the offending
    task itself only dies later, reasonless, via poison quarantine.

    So: coerce defensively and return ``(value, None)`` on success or
    ``(None, error)`` on a value that cannot be a VRAM figure — the caller
    fails THAT task with a message naming the bad field and value, and the cycle
    survives. ``None`` (unset) is not an error: it simply declares no VRAM floor.

    Non-finite values are rejected too, not just non-numeric ones: ``inf`` parses
    fine but blows up downstream at ``int(vram_gb * 1024)`` (OverflowError) in
    :func:`_select_eligible_runner`, which is the same hot-path crash by a longer
    route. ``bool`` is rejected explicitly — it is an ``int`` subclass, so
    ``float(True) == 1.0`` would otherwise silently mean "1 GB".
    """
    raw = payload.get("vram_gb") or (payload.get("compute_experiment") or {}).get(
        "vram_gb"
    )
    if raw is None:
        return None, None
    if isinstance(raw, bool):
        return None, (
            f"vram_gb must be a number of gigabytes (e.g. 24 or 24.5); "
            f"got boolean {raw!r}"
        )
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except (ValueError, TypeError):
            return None, (
                f"vram_gb must be a number of gigabytes (e.g. 24 or 24.5); got "
                f"{raw!r} — drop the unit suffix and requeue"
            )
    else:
        return None, (
            f"vram_gb must be a number of gigabytes (e.g. 24 or 24.5); got "
            f"{type(raw).__name__} {raw!r}"
        )
    if not math.isfinite(value):
        return None, (
            f"vram_gb must be a finite number of gigabytes (e.g. 24 or 24.5); "
            f"got {raw!r}"
        )
    return value, None


def _coerce_min_compute_capability(payload: dict) -> tuple[float | None, str | None]:
    """Resolve an image's ``min_compute_capability`` floor to a float, or an error.

    ``min_compute_capability`` is the CUDA compute-capability an image's bundled
    GPU wheels require (e.g. ``8.0`` for an ``sm_80``-built image). It is the same
    class of AGENT/OPERATOR-AUTHORED free-form payload data as ``vram_gb`` and is
    read in the dispatch hot path, so it gets the identical defensive contract: a
    bare ``float(raw)`` would raise on the values authors actually write (``"8.0"``
    with a stray unit, ``[8.0]``, ``{}``) and that exception would propagate out of
    the dispatch cycle, taking down dispatch for EVERY task in the cycle while the
    offending task dies later, reasonless, in poison quarantine.

    Returns ``(value, None)`` on success or ``(None, error)`` on a value that
    cannot be a compute-capability figure — the caller fails THAT task with a
    message naming the bad field and the cycle survives. ``None`` (unset) and a
    non-positive figure both mean "no floor" and are not errors. ``bool`` is
    rejected explicitly (an ``int`` subclass, so ``float(True) == 1.0`` would
    silently mean a 1.0 floor).
    """
    raw = payload.get("min_compute_capability") or (
        payload.get("compute_experiment") or {}
    ).get("min_compute_capability")
    if raw is None:
        return None, None
    if isinstance(raw, bool):
        return None, (
            f"min_compute_capability must be a CUDA compute-capability number "
            f"(e.g. 7.0 or 8.9); got boolean {raw!r}"
        )
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except (ValueError, TypeError):
            return None, (
                f"min_compute_capability must be a CUDA compute-capability number "
                f"(e.g. 7.0 or 8.9); got {raw!r} — drop the unit suffix and requeue"
            )
    else:
        return None, (
            f"min_compute_capability must be a CUDA compute-capability number "
            f"(e.g. 7.0 or 8.9); got {type(raw).__name__} {raw!r}"
        )
    if not math.isfinite(value):
        return None, (
            f"min_compute_capability must be a finite CUDA compute-capability "
            f"number (e.g. 7.0 or 8.9); got {raw!r}"
        )
    if value <= 0:
        return None, None  # non-positive floor == no floor
    return value, None


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
    _task_is_authoring,
    _task_requires_project_verification,
)

# Free-disk floor a runner must clear to be sent a container-image compute job.
# A baked compute image is multi-GB and must be PULLED before the job starts;
# a runner below this floor would die mid-pull ("no space left on device"),
# wasting the claim. This is a DISPATCH-FIT floor distinct from the (smaller)
# ``_RUNNER_LOW_DISK_THRESHOLD_BYTES_DEFAULT`` alert threshold — the alert warns
# an operator, this gate keeps a doomed job from ever being claimed. Overridable
# via runtime_config ``container_image_min_free_disk_bytes``.
# Missing workspace/image-store telemetry is treated as eligible (backward
# compatible). When reported, the image store must clear the pull floor while
# the workspace clears its smaller checkout/artifact reserve.
_CONTAINER_IMAGE_MIN_FREE_DISK_BYTES_DEFAULT = 10_737_418_240  # 10 GiB
_CONTAINER_WORKSPACE_MIN_FREE_DISK_BYTES_DEFAULT = 2_147_483_648  # 2 GiB


def _container_image_min_free_disk_bytes() -> int:
    """The dispatch-fit free-disk floor for container-image jobs (config-overridable)."""
    try:
        return int(
            _runtime_config.get(
                "container_image_min_free_disk_bytes",
                _CONTAINER_IMAGE_MIN_FREE_DISK_BYTES_DEFAULT,
            )
        )
    except (TypeError, ValueError):
        return _CONTAINER_IMAGE_MIN_FREE_DISK_BYTES_DEFAULT


def _container_workspace_min_free_disk_bytes() -> int:
    """Workspace reserve, preserving the legacy single-floor override."""
    try:
        if "container_workspace_min_free_disk_bytes" not in _runtime_config:
            # Before split-volume telemetry, the image floor governed the only
            # known filesystem. Preserve an explicitly configured legacy value
            # (including zero) until an operator sets the new workspace key.
            if "container_image_min_free_disk_bytes" in _runtime_config:
                return max(
                    0,
                    int(_runtime_config["container_image_min_free_disk_bytes"]),
                )
            return _CONTAINER_WORKSPACE_MIN_FREE_DISK_BYTES_DEFAULT
        return max(
            0,
            int(_runtime_config["container_workspace_min_free_disk_bytes"]),
        )
    except (TypeError, ValueError):
        return _CONTAINER_WORKSPACE_MIN_FREE_DISK_BYTES_DEFAULT


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


_BROWSER_EVIDENCE_KINDS = frozenset(
    {"browser", "playwright", "screenshot", "viewport", "visual"}
)
_BROWSER_RUNTIME_PATHS = frozenset(
    {
        "admin-ui/package.json",
        "admin-ui/package-lock.json",
        "admin-ui/vite.config.ts",
        "admin-ui/vitest.config.ts",
        "admin-ui/vitest.viewport.config.ts",
        "admin-ui/eslint.config.js",
    }
)
_BROWSER_RUNTIME_PREFIXES = ("admin-ui/src/", "admin-ui/eslint-rules/")


def _paths_require_browser(paths: object) -> bool:
    """Return whether a declared/observed path set enters the viewport gate."""
    if not isinstance(paths, (list, tuple, set)):
        return False
    return any(
        isinstance(path, str)
        and (
            path.startswith(_BROWSER_RUNTIME_PREFIXES)
            or path in _BROWSER_RUNTIME_PATHS
        )
        for path in paths
    )


def _payload_requires_browser(payload: dict, context: dict) -> bool:
    """Read the browser contract from payload and task path provenance.

    ``related_files`` is available before an agent starts (the dispatch-time
    authoring scope), while ``_pr_changed_files`` is the authoritative observed
    diff once GitHub has reported it. Either path set must trigger the same
    ``check:107`` admin-ui gate; missing provenance never grants capability.
    """
    if payload.get("requires_browser") is True:
        return True
    if _paths_require_browser(payload.get("related_files")):
        return True
    if _paths_require_browser((context or {}).get("related_files")):
        return True
    if _paths_require_browser((context or {}).get("_pr_changed_files")):
        return True
    evidence = payload.get("acceptance_evidence")
    if not isinstance(evidence, dict):
        return False
    return str(evidence.get("kind") or "").strip().lower() in _BROWSER_EVIDENCE_KINDS


def _task_requires_browser(task: Task) -> bool:
    """Evaluate the browser contract using the task's complete provenance."""
    return _payload_requires_browser(task.first_payload or {}, task.context or {})


def _runner_lacks_required_browser(runner: LocalRunner, task: Task) -> bool:
    """Return whether ``runner`` must refuse this task's browser contract."""
    return not _runner_chromium_usable(runner) and _task_requires_browser(task)

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
        project is not None
        and _task_is_authoring(task_type)
        and (
            is_deploy_repo
            or _task_requires_project_verification(task_type, project.build_config)
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
        requires_browser=_task_requires_browser(task),
    )
    if runner is None:
        return False

    new_ctx = dict(ctx)
    new_ctx["runner_target_id"] = str(runner.id)
    new_ctx["runner_hostname"] = runner.hostname
    # A task that was held (enforce-local) and is now pinned must not carry the
    # stale wait stamp — otherwise the bounded-timeout sweep could fail it after
    # it has already been claimed by a runner.
    new_ctx.pop(LOCAL_RUNNER_HOLD_CONTEXT_KEY, None)
    new_ctx.pop(CONTAINER_RUNNER_HOLD_CONTEXT_KEY, None)
    new_ctx.pop(STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY, None)
    new_ctx.pop("runner_pin_hold", None)
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
    min_compute_capability: float | None = None,
    vram_gb: float | None = None,
    declined_record: dict | None = None,
    requires_project_verification: bool = False,
) -> tuple[str, str]:
    """Explain why a strict-pinned runner cannot take this job RIGHT NOW.

    Returns ``(reason_code, detail)``. The reason must be truthful and specific
    (runner id + why), never a generic no-runner message: reason codes are
    ``declined_recently`` / ``at_capacity`` / ``task_type_not_allowed`` /
    ``missing_project_verification`` / ``missing_gpu`` / ``missing_container_runtime`` / ``insufficient_vram`` / ``not_eligible`` (live but
    failed another gate) for runners in the live cache, and ``not_found`` /
    ``not_enrolled`` / ``offline`` for runners that are not live. The pair is
    stamped into ``task.context['runner_pin_hold']`` and the hold status message
    so the admin UI can show exactly why nothing is dispatching.
    """
    # Checked FIRST, mirroring _select_eligible_runner: the decline exclusion is
    # applied to the candidate pool BEFORE any eligibility filter, so a runner it
    # dropped never reaches the capacity/task-type/GPU gates below. Reporting one
    # of those instead would be a lie, and reporting the generic "not_eligible"
    # would leave the operator with no idea the runner had said no — and no
    # sight of the reason the runner itself gave.
    if declined_record is not None:
        count = declined_record.get("count") or 1
        why = declined_record.get("reason") or "unspecified"
        left = declined_record.get("seconds_left")
        left_txt = f"; excluded for another {left}s" if left is not None else ""
        return (
            "declined_recently",
            f"runner declined this task {count}x (most recent reason: {why})"
            f"{left_txt}",
        )

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
        if requires_project_verification and not _runner_has_project_verification(
            snapshot
        ):
            return (
                "missing_project_verification",
                "runner has no valid v1 write-agent proof for python3 -m ruff "
                "and live Docker/Postgres",
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
        if container_image:
            image_store_floor = _container_image_min_free_disk_bytes()
            workspace_floor = _container_workspace_min_free_disk_bytes()
            if not _runner_has_free_disk(
                snapshot,
                image_store_floor,
                workspace_required_bytes=workspace_floor,
                container_image=container_image,
            ):
                storage = _runner_container_image_storage(snapshot)
                # Mirror the selection gate: an image already in the runner's
                # local store waives the pull floor, so the only floor that can
                # still fail is workspace scratch — say that, not a bogus
                # multi-GB-pull message about a pull that would never happen.
                if _runner_container_image_present(snapshot, container_image):
                    workspace_only_floor = _runner_effective_workspace_disk_floor(
                        snapshot,
                        0,
                        workspace_required_bytes=workspace_floor,
                    )
                    return (
                        "insufficient_disk",
                        f"job declares container_image {container_image!r} "
                        "which is already present in the runner's local image "
                        "store (no pull needed), but the runner has "
                        f"workspace_free_bytes={storage['workspace_free_bytes']}"
                        " and checkout/artifact scratch requires the workspace "
                        f"to clear {workspace_only_floor} bytes",
                    )
                effective_workspace_floor = _runner_effective_workspace_disk_floor(
                    snapshot,
                    image_store_floor,
                    workspace_required_bytes=workspace_floor,
                )
                runtime = storage["image_store_runtime"] or "container"
                image_path = storage["image_store_path"] or "unknown path"
                legacy_detail = (
                    "; no separate image-store telemetry is available, so the "
                    "legacy workspace/disk measurement must also clear the "
                    f"{image_store_floor}-byte image-pull floor"
                    if storage["image_store_free_bytes"] is None
                    else ""
                )
                return (
                    "insufficient_disk",
                    f"job declares container_image {container_image!r} (a multi-GB "
                    "image pull) but the runner has "
                    f"workspace_free_bytes={storage['workspace_free_bytes']} and "
                    f"{runtime}_image_store_free_bytes="
                    f"{storage['image_store_free_bytes']} at {image_path}; workspace "
                    f"must clear {effective_workspace_floor} bytes and image store "
                    f"must clear {image_store_floor} bytes{legacy_detail}",
                )
        if min_compute_capability and not _runner_meets_min_compute_capability(
            snapshot, min_compute_capability
        ):
            reported = (snapshot.capabilities or {}).get("gpu_compute_capabilities")
            return (
                "incompatible_gpu_arch",
                f"job requires GPU compute capability >= {min_compute_capability} "
                f"but the runner's GPUs report {reported} — the image cannot run on "
                "this architecture",
            )
        if _job_requires_gpu_in_container(
            accelerator, container_image, vram_gb
        ) and not _runner_gpu_in_container_capable(snapshot):
            gpu_req = (
                f"accelerator {accelerator!r}"
                if accelerator
                else f"vram_gb {vram_gb!r}"
            )
            return (
                "missing_gpu_in_container_capability",
                f"job declares both container_image {container_image!r} and "
                f"{gpu_req} but GPU passthrough is not verified: "
                f"{_runner_gpu_passthrough_blocker(snapshot)}",
            )
        if (
            vram_gb is not None
            and vram_gb > 0
            and not _runner_has_free_vram(snapshot, int(vram_gb * 1024))
        ):
            reported = (snapshot.capabilities or {}).get("gpu_free_vram_mib")
            return (
                "insufficient_vram",
                f"job requires {vram_gb} GB free VRAM but the runner's GPUs "
                f"report gpu_free_vram_mib={reported}",
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
    # Enrollment now means allow-list membership, not the single home project: a
    # multi-project runner bound to this project is validly pinnable even when its
    # home project_id is a different one.
    if project_id not in await allowed_project_ids(session, row):
        return (
            "not_enrolled",
            "runner is not on this project's allow-list — a pin must name one "
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
    if runner is None:
        return None
    # Membership, not the single home project: a multi-project runner pinned to
    # any allowed project still resolves its hostname for the "waiting for <host>"
    # hold message.
    if project_id not in await allowed_project_ids(session, runner):
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
    min_compute_capability: float | None = None,
    declined_runner_ids: set[str] | None = None,
    declined_exclusion_advisory: bool = False,
    requires_project_verification: bool = False,
    requires_browser: bool = False,
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
    When a ``min_compute_capability`` floor is declared (an image's GPU-arch
    requirement) a runner must have at least one GPU whose reported
    ``gpu_compute_capabilities`` meets it — an older-arch box (a V100 for an
    ``sm_80`` image) is excluded so the job holds for compatible hardware instead
    of dying in the runner's reactive in-sandbox probe.
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

    ``declined_runner_ids`` are runners that recently DECLINED this specific task
    (see :mod:`app.runner_decline`). They are removed from the candidate pool
    because that same stable per-task tiebreak would otherwise re-pick the runner
    that just declined — pin → pull → decline → pin, forever. Excluding them is
    what makes the loop terminate: the task moves to other hardware, or to no
    runner at all, which correctly falls through to cloud dispatch.

    ``declined_exclusion_advisory`` waives that exclusion when honouring it would
    empty the candidate set (see ``runner_decline.decline_exclusion_is_advisory``,
    which sets it only for a Strategic task whose declines still look transient).
    For a normal task an empty set IS the graceful exit — cloud dispatch. For a
    local-required Strategic task it is not: the task parks and the 900s hold
    sweep evicts it to cloud, defeating continuous-local mode. So rather than
    empty the pool we re-pick the decliner and let the retry try again; the
    decline counter and operator alert still escalate, and the waiver stops at
    ``DECLINE_ESCALATION_THRESHOLD`` so a genuinely incapable single-runner fleet
    still reaches the hold → cloud-fallback safety valve instead of livelocking.
    Whenever other hardware exists the exclusion is honoured unconditionally.

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

    def _eligible(pool: list) -> list:
        """Runners in ``pool`` that can actually take this task THIS cycle."""
        out = [
            r
            for r in pool
            if _runner_accepting_new_work(r)
            and _runner_handles_task_type(r, task_type)
            and (task_type == "compute_experiment" or _runner_agent_available(r))
            and not _runner_at_capacity(r, pins.get(r.id, 0), task_type)
        ]
        if requires_project_verification:
            out = [r for r in out if _runner_has_project_verification(r)]
        if requires_browser:
            # Browser-required work is fail-safe: only a current, explicit runner
            # proof is accepted. Older runners omit this capability and therefore
            # fall through to the Packer-proven cloud image instead of claiming a
            # task that will fail after Chromium bootstrap. Reads the NESTED
            # isolation_backends Docker + Playwright cache evidence the heartbeat
            # writes (always plain bools), requiring both legs.
            out = [r for r in out if _runner_chromium_usable(r)]
        if accelerator:
            # Trust VERIFIED GPU capability, not the bare self-reported has_gpu bool:
            # a mis-set flag would silently defeat this gate and win GPU jobs the
            # runner cannot run. _runner_gpu_capable cross-checks has_gpu against the
            # runner's own nvidia-smi telemetry (gpu_count/gpu_names) and disbelieves
            # a claim its telemetry contradicts. Fail-safe: a runner that cannot be
            # verified GPU-capable falls out of `eligible` and the task holds for
            # real hardware.
            out = [r for r in out if _runner_gpu_capable(r)]
        if container_image:
            out = [r for r in out if _runner_has_container_capability(r)]
            # Disk-fit gate: a baked container image is multi-GB and must be
            # pulled before the job starts. Exclude runners whose reported free
            # disk is below the fit floor so a doomed pull is never claimed —
            # the job holds for a runner that can fit it (or the cloud backend)
            # instead of dying mid-pull. Runners that do not report
            # disk_free_bytes are not gated (backward compatible). When the
            # runner reports the requested image ALREADY in its local store
            # (container_images_present), the pull is a no-op and only the
            # smaller workspace scratch floor applies — a dgx-class runner
            # permanently below the pull floor can still run its cached image.
            image_store_floor = _container_image_min_free_disk_bytes()
            workspace_floor = _container_workspace_min_free_disk_bytes()
            out = [
                r
                for r in out
                if _runner_has_free_disk(
                    r,
                    image_store_floor,
                    workspace_required_bytes=workspace_floor,
                    container_image=container_image,
                )
            ]
        if _job_requires_gpu_in_container(accelerator, container_image, vram_gb):
            # Combined gate: when a container image is required AND the job asks
            # for a GPU (an explicit accelerator OR a positive vram_gb GPU-memory
            # floor — VRAM lives only on a GPU), the runner must be GPU-in-container
            # capable — merely having a container runtime AND a GPU independently is
            # not enough (the job would launch in the container and fail at runtime
            # with "no supported GPU detected"). A CDI daemon (docker_cdi_gpu) or an
            # nvidia-hook docker runner (has_gpu verified) both satisfy this gate;
            # non-docker containers (podman, apptainer, enroot) handle GPU
            # passthrough natively.
            out = [r for r in out if _runner_gpu_in_container_capable(r)]
        if vram_gb is not None and vram_gb > 0:
            required_mib = int(vram_gb * 1024)
            out = [r for r in out if _runner_has_free_vram(r, required_mib)]
        if min_compute_capability is not None and min_compute_capability > 0:
            # GPU-arch gate: an image built for a newer architecture (its bundled
            # CUDA/torch wheels carry SASS only for that floor) cannot run on an
            # older GPU. Exclude runners whose reported gpu_compute_capabilities are
            # all below the image's floor so the job holds for compatible hardware
            # instead of being sent and dying in the reactive in-sandbox probe.
            # Runners that do not report the telemetry are not gated (backward
            # compatible; the in-sandbox probe stays their safety net).
            out = [
                r
                for r in out
                if _runner_meets_min_compute_capability(r, min_compute_capability)
            ]
        return out

    if declined_runner_ids and not forced_runner_target_id:
        # Not applied under a forced pin: an operator hard-pin is an explicit
        # instruction to use that runner and nothing else, so it out-ranks the
        # decline heuristic (the hold + decline alert still surface the problem).
        eligible = _eligible(
            [r for r in candidates if str(r.id) not in declined_runner_ids]
        )
        # The waiver is decided on ELIGIBILITY, not on whether some other runner
        # ROW merely exists. Deciding it on the raw candidate list made the waiver
        # inert on every multi-runner fleet — including the live one: with a
        # Strategic laptop + a dgx-1, excluding the laptop leaves dgx-1 "surviving",
        # so the exclusion was applied — and dgx-1 then fails the task-type /
        # capacity gate, `eligible` is empty, and the Strategic task is parked and
        # evicted to cloud by the hold sweep. Exactly what the waiver exists to
        # prevent, defeated by a runner that could never have taken the task.
        #
        # So: fall back to the decliner only when no ELIGIBLE runner survives.
        if not eligible and declined_exclusion_advisory:
            eligible = _eligible(candidates)
    else:
        eligible = _eligible(candidates)

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
    payload: dict,
    *,
    command: str,
    spend_cap_usd: float,
    deadline_seconds: int,
    container_image_override: str | None = None,
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

    ``container_image_override`` is the project's digest-pinned baked image the
    caller resolved because the payload declared no ``container_image``; it is
    forwarded ONLY when the payload has none (an explicit payload image wins).
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
    elif _present(container_image_override):
        # USE-default: the payload declared no container_image, so dispatch
        # resolved the project's digest-pinned baked image. An explicit payload
        # image always wins over the default (checked first above).
        kwargs["container_image"] = container_image_override
    if isinstance(payload.get("container_registry_auth"), dict):
        kwargs["container_registry_auth"] = payload["container_registry_auth"]
    if payload.get("allow_host_fallback"):
        kwargs["allow_host_fallback"] = bool(payload["allow_host_fallback"])
    # No-progress watchdog window (peer of runtime deadline). Forwarded when set so
    # a stalled job is cut early; create_compute_job validates/clamps it. Integer
    # field, so it uses a plain presence check rather than the string `_present`.
    nested_compute_experiment = payload.get("compute_experiment") or {}
    no_progress_timeout = payload.get("no_progress_timeout_seconds")
    if no_progress_timeout is None:
        no_progress_timeout = nested_compute_experiment.get(
            "no_progress_timeout_seconds"
        )
    if no_progress_timeout is not None:
        kwargs["no_progress_timeout_seconds"] = no_progress_timeout
    # Cross-job checkpoint resume: forward the optional predecessor reference so the
    # new job rehydrates that job's durable bundle before its command runs.
    # create_compute_job does the authoritative same-project + bundle-exists check.
    input_artifact_job_id = payload.get("input_artifact_job_id") or nested_compute_experiment.get(
        "input_artifact_job_id"
    )
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


async def _defer_compute_experiment_task(
    session: AsyncSession, task: Task, message: str
) -> str:
    """DEFER a ``compute_experiment`` whose launch hit a TRANSIENT precondition.

    The project's compute-job concurrency cap is not a failure — it is back-pressure.
    Active jobs are a strictly shrinking set absent new launches, so the very same
    task launches unchanged once a running job reaches a terminal state. Terminal-
    FAILing it (the old behaviour for every non-live-release precondition) burned the
    direction's failure budget for a queueing decision the orchestrator itself made,
    and fed the churn guardrail with phantom failures.

    So: keep the task QUEUED and stamp the established ``retry_after_until`` backoff
    (``set_retry_after`` + ``_infra_wait``) — the same convention the provisioner uses
    for infrastructure back-pressure. The candidate scan at the top of the dispatch
    cycle skips a task whose ``retry_after_until`` is in the future and clears the
    marker once it lapses, so the task re-enters dispatch on its own with no sweep and
    no retry-budget burn. Backoff escalates per consecutive deferral (2 → 15 min).

    Deliberately does NOT call ``record_task_result`` — a deferral is not an outcome,
    and recording one would recreate the exact failure-budget pollution this fixes.

    Returns ``"deferred"``.
    """
    from app.broadcast import broadcast_to_project
    from app.time_utils import set_retry_after

    ctx = dict(task.context or {})
    # _infra_wait marks this as back-pressure, not a rate-limit hold: it stops the
    # candidate scan from releasing the backoff early on LLM-account recovery (an
    # available account does nothing for a full compute-job cap).
    ctx["_infra_wait"] = True
    ctx[COMPUTE_LAUNCH_DEFERRED_REASON_CONTEXT_KEY] = message[:500]
    try:
        _prior_defers = int(ctx.get(COMPUTE_LAUNCH_DEFER_COUNT_CONTEXT_KEY) or 0)
    except (TypeError, ValueError):
        _prior_defers = 0
    ctx[COMPUTE_LAUNCH_DEFER_COUNT_CONTEXT_KEY] = _prior_defers + 1
    task.context = ctx
    # Clamp the EXPONENT, not just the product. `float * (2**n)` raises
    # OverflowError("int too large to convert to float") once n >= 1024 — and the
    # raise happens inside _dispatch_compute_experiment_directly, i.e. inside the
    # dispatch loop's per-task containment, so a task that was CORRECTLY deferring
    # would start crashing every cycle and get QUARANTINED as a poison task. That is
    # reachable: a project pinned at its compute cap defers every ~15 min, so the
    # counter passes 1024 after ~10.6 days. The backoff is already saturated at
    # _COMPUTE_CAP_DEFER_MAX_MINUTES long before n=8, so clamping there is
    # value-identical for every n and merely keeps the arithmetic finite.
    backoff_minutes = min(
        _COMPUTE_CAP_DEFER_BASE_MINUTES * (2 ** min(_prior_defers, 8)),
        _COMPUTE_CAP_DEFER_MAX_MINUTES,
    )
    # set_retry_after rewrites task.context off the dict above, so the keys just set
    # survive; it owns retry_after_until + the shared _retry_after_resets counter.
    set_retry_after(task, backoff_minutes)
    task.status = TaskStatus.QUEUED  # explicit: a deferral never leaves QUEUED
    task.latest_status_message = message[:500]
    session.add(task)
    # Operator surface for a deferral that is never going to clear on its own.
    #
    # Deferring instead of terminal-FAILing removed a LOUD (but wrong) signal; without
    # this it would be replaced by a SILENT one — the task parks behind a saturated
    # 15-min backoff forever, and nothing surfaces it: the counter only resets on a
    # successful launch, and operator_attention's stale-queued sweep filters on
    # `jsonb_array_length(depends_on) > 0`, so a dep-less compute_experiment never
    # appears there. Past the saturation point the cap is no longer transient
    # back-pressure in any useful sense — it is a standing condition an operator
    # should see. Staged BEFORE the commit below so it costs no extra transaction.
    if _prior_defers + 1 >= _COMPUTE_CAP_DEFER_ALERT_AFTER_DEFERS:
        try:
            await _maybe_alert_saturated_compute_cap_defer(
                task, session, message, _prior_defers + 1
            )
        except Exception:
            # An alerting failure must never turn a correct deferral into a crash
            # (which the dispatch loop's containment would quarantine).
            logger.exception(
                "[ORCHESTRATOR] failed to stage saturated compute-cap defer alert "
                "for task %s",
                task.id,
            )
    # Snapshot broadcast scalars BEFORE commit — commit expires ORM state and a
    # post-commit task.id/.project_id read lazy-loads from async context
    # (MissingGreenlet on asyncpg), which would quarantine a correctly-deferred task.
    _deferred_project_id = str(task.project_id)
    _deferred_task_id = str(task.id)
    await session.commit()
    try:
        await broadcast_to_project(
            _deferred_project_id,
            "task_updated",
            {
                "task_id": _deferred_task_id,
                "status": TaskStatus.QUEUED,
                "message": message[:200],
            },
        )
    except Exception:
        logger.exception(
            "[ORCHESTRATOR] task_updated broadcast failed for deferred "
            "compute_experiment %s",
            _deferred_task_id,
        )
    logger.info(
        "[ORCHESTRATOR] compute_experiment %s DEFERRED on transient launch "
        "precondition (retry in %.1f min, still QUEUED): %s",
        _deferred_task_id,
        backoff_minutes,
        message[:200],
    )
    return "deferred"


async def _maybe_alert_saturated_compute_cap_defer(
    task: Task,
    session: AsyncSession,
    message: str,
    defer_count: int,
) -> None:
    """Stage ONE deduplicated WARNING alert for a compute_experiment stuck at the cap.

    Deduplicates on (task, stable title) so the alert is raised once and does not
    re-fire on each subsequent deferral. WARNING (not ERROR): the task is healthy and
    correctly queued — the *project* is saturated, and that is an operator/capacity
    decision (raise ``max_concurrent_compute_jobs_by_project``, or wait for the running
    jobs). Never records a task result: a deferral is still not an outcome.

    The dedup predicate excludes DISMISSED alerts (matching the sibling
    retry_after "task stuck" alert). Without that, an operator dismissing this once
    would silence it FOREVER for that task — and since a deferral is unbounded by
    design, the task could keep deferring indefinitely with the operator's only
    surface permanently suppressed. Dismissing means "I have seen this", not "never
    tell me again"; if the condition persists past a dismissal it is allowed to
    re-raise.

    Mirrors ``_create_strategic_local_cloud_fallback_alert``: stages the row and
    broadcasts; the CALLER owns the commit.
    """
    from app.models import Alert, AlertSeverity

    existing = await session.execute(
        select(Alert).where(
            Alert.task_id == task.id,
            Alert.title == _COMPUTE_CAP_DEFER_ALERT_TITLE,
            Alert.dismissed_at.is_(None),
        )
    )
    if existing.scalars().first() is not None:
        return
    alert = Alert(
        project_id=task.project_id,
        severity=AlertSeverity.WARNING,
        title=_COMPUTE_CAP_DEFER_ALERT_TITLE,
        message=(
            f"compute_experiment has been deferred {defer_count}x on the project's "
            f"compute-job concurrency cap and is now at the maximum "
            f"{_COMPUTE_CAP_DEFER_MAX_MINUTES:.0f}-minute backoff. It is still QUEUED "
            "and will launch on its own as soon as a running job finishes — no action "
            "is required if the current jobs are expected to complete. If they are "
            "long-running or stuck, raise the project's compute-job cap "
            "(max_concurrent_compute_jobs_by_project) or cancel a running job. "
            f"Launch precondition: {message[:200]}"
        ),
        task_id=task.id,
        event_type="capacity.quota_exhausted",
    )
    session.add(alert)
    _alert_project_id = str(task.project_id)
    try:
        from app.broadcast import broadcast_to_project

        await broadcast_to_project(
            _alert_project_id,
            "alert_created",
            {
                "alert_id": str(alert.id),
                "severity": "WARNING",
                "title": alert.title,
            },
        )
    except Exception:
        logger.exception(
            "[ORCHESTRATOR] alert_created broadcast failed for saturated compute-cap "
            "defer on task %s",
            task.id,
        )


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
        ``"launched"``  — ComputeJob created, task PARKED on the job-terminal wake.
        ``"failed"``    — launch misconfigured / non-recoverable precondition; task
                          terminal-FAILED.
        ``"blocked"``   — a recoverable live-release precondition (release not yet
                          serving-confirmed); task BLOCKED, auto-unblock sweep re-queues
                          it once the deploy confirms (not a failure, no retry burn).
        ``"held"``      — held QUEUED (``awaiting_local_runner_since`` stamped; the
                          existing bounded sweep terminal-fails a hold that outlives
                          the wait window). Two hold causes: ``require_local_runner``
                          set and no live runner, or a strict ``runner_target`` pin
                          whose runner is not currently eligible (the latter also
                          stamps ``context['runner_pin_hold']`` with the
                          machine-readable reason).
        ``"deferred"``  — a TRANSIENT launch precondition (the project's compute-job
                          concurrency cap); task stays QUEUED behind a ``retry_after``
                          backoff and re-enters dispatch on its own once the cap frees.
                          NOT a failure — no failure budget burned.
    Only ``"launched"`` and ``"failed"`` count as a dispatch; ``"blocked"`` / ``"held"``
    / ``"deferred"`` leave the task waiting and must NOT consume a dispatch-window slot
    (see the backfill in the orchestrator's dispatch loop). The caller owns the dispatch
    loop; this function commits its own outcome.
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

    # Placement-policy resolvers are imported at module scope (see the top-of-file
    # app.resource_envelope import).  Legacy require_local_runner remains a
    # compute-experiment-only compatibility rule; the new local_only policy is
    # task-wide and never converts runner absence into a failure.

    task_id = task.id
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
    # ── USE-default (images-first): default the project's declared image ───────
    # A compute_experiment dispatched WITHOUT an explicit container_image — an
    # operator-created experiment, a planned compute_experiment prerequisite, or a
    # retry that never went through the plan/research completion authors — reaches
    # here image-less and would run unreproducibly on the host toolchain. Resolve
    # the project's declared image. External runtime images (no build recipe) are
    # launched as declared; Blueprint-built images require a resolved,
    # digest-pinned build record. This runs BEFORE runner selection so the image
    # flows through the container-runtime / GPU-in-container eligibility gate
    # below. An explicit payload container_image is left untouched; projects with
    # no manifest compute image keep today's image-less behavior.
    resolved_default_container_image: str | None = None
    if not container_image:
        from app.compute_backends import resolve_project_baked_image_strict

        project = await session.get(Project, task.project_id)
        resolved_default_container_image, image_error = (
            await resolve_project_baked_image_strict(session, project)
        )
        if image_error is not None:
            return await _fail_compute_experiment_task(
                session,
                task,
                "baked_image_unresolvable",
                image_error,
            )
        if resolved_default_container_image:
            container_image = resolved_default_container_image
    # vram_gb (flat or nested) gates runner eligibility on free GPU memory. It is
    # AGENT-AUTHORED, so a bad value ("24GB") must fail THIS task with an honest
    # validation message — never raise in the dispatch hot path, which would abort
    # the cycle for every other task and leave this one to die reasonless in poison
    # quarantine. Same terminal-fail contract as the config_errors check above.
    vram_gb, vram_error = _coerce_vram_gb(payload)
    if vram_error is not None:
        return await _fail_compute_experiment_task(
            session,
            task,
            "compute_launch_failed",
            "Compute experiment misconfigured: " + vram_error,
        )
    # min_compute_capability (flat or nested) is the image's GPU-arch floor; it
    # gates runner eligibility on gpu_compute_capabilities. AGENT/OPERATOR-AUTHORED,
    # so a bad value fails THIS task with an honest message (never raises in the
    # dispatch hot path). Same terminal-fail contract as the vram_gb check above.
    min_compute_capability, min_cc_error = _coerce_min_compute_capability(payload)
    if min_cc_error is not None:
        return await _fail_compute_experiment_task(
            session,
            task,
            "compute_launch_failed",
            "Compute experiment misconfigured: " + min_cc_error,
        )

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
        min_compute_capability=min_compute_capability,
        declined_runner_ids=recently_declined_runner_ids(task),
        declined_exclusion_advisory=decline_exclusion_is_advisory(
            task, "compute_experiment"
        ),
    )

    # Resolve the task-wide contract before any of the legacy specialized holds.
    # Some of those markers intentionally have bounded timeout semantics for
    # old compatibility behavior.  An explicit local_only policy must instead
    # get the durable structured wait regardless of whether the immediate
    # blocker is a strict pin, a forced host, or missing container/GPU capacity.
    if runner is None and await _hold_task_for_local_only_placement(
        session,
        task,
        reason="no_eligible_local_runner",
    ):
        logger.info(
            "[RUNNERS] Holding compute_experiment %s: project placement policy "
            "is local_only and no live eligible runner is available",
            task_id,
        )
        return "held"

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
            min_compute_capability=min_compute_capability,
            vram_gb=vram_gb,
            # A strict `runner_target` pin is passed to _select_eligible_runner as
            # a SOFT preference, so the decline exclusion DOES drop the pinned
            # runner from the candidate pool (unlike an operator hard pin, which
            # short-circuits it). Without this the diagnosis would blame a generic
            # "not_eligible" for a runner that is perfectly healthy and simply
            # declined the task — the exact observability hole this campaign is
            # closing. Not consulted under a forced hard pin, mirroring selection.
            declined_record=(
                None
                if forced_runner_target_id
                else decline_record_for_runner(task, pin_runner_id)
            ),
        )
        message = (
            f"runner_target hold: strict pin to runner {pin_runner_id} cannot "
            f"dispatch — {reason_code}: {detail}"
        )[:500]
        ctx = dict(task.context or {})
        changed = ctx.pop("runner_target_id", None) is not None  # drop stale pin
        if not ctx.get(LOCAL_RUNNER_HOLD_CONTEXT_KEY):
            ctx[LOCAL_RUNNER_HOLD_CONTEXT_KEY] = _utcnow().isoformat()
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
        reason_code, detail = await _diagnose_strict_runner_pin_hold(
            session,
            active_runners_cache,
            task.project_id,
            forced_id,
            "compute_experiment",
            accelerator,
            container_image,
            runner_pins_this_cycle,
            min_compute_capability=min_compute_capability,
            vram_gb=vram_gb,
        )
        if reason_code == "missing_gpu_in_container_capability":
            hold_msg = (
                f"Waiting for forced runner {target_label}: this compute experiment "
                "is hard-pinned to that runner; GPU inside the container cannot be "
                f"fulfilled because {detail}."
            )
        else:
            hold_msg = (
                f"Waiting for forced runner {target_label}: this compute experiment is "
                "hard-pinned to that runner and will not run on any other runner or in "
                f"the cloud until it is live and eligible ({reason_code}: {detail})."
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
            "this cycle (%s: %s) — not dispatching elsewhere",
            task.id,
            target_label,
            reason_code,
            detail,
        )
        return "held"

    project = await session.get(Project, task.project_id)
    require_local = bool(
        project is not None and resolve_project_require_local_runner(project)
    )

    # ── GPU-arch hold: a min_compute_capability floor is declared and at least one
    # live runner has a GPU but its architecture is too OLD to run the image
    # (gpu_compute_capabilities all below the floor) → keep QUEUED with an honest
    # "awaiting GPU with compute capability >= X.Y" reason instead of dispatching
    # to a box where the job would die in the reactive in-sandbox probe. Fires only
    # when a GPU-capable-but-wrong-arch runner exists (`arch_blocked`), so the arch
    # floor is a genuine blocker for this fleet and not a stand-in for a more
    # fundamental gate (no GPU at all, no container runtime — those own their own
    # holds/fallbacks). Placed BEFORE the container-image hold so an
    # otherwise-container-capable runner that fails only on GPU arch reports the
    # accurate reason. Reuses the container-runner hold stamp so the existing
    # bounded-wait sweep (15-min early alert, 6h terminal fail) covers it unchanged.
    if runner is None and min_compute_capability:
        project_runners = active_runners_cache.get(task.project_id) or []
        arch_blocked = any(
            _runner_gpu_capable(r)
            and not _runner_meets_min_compute_capability(r, min_compute_capability)
            for r in project_runners
        )
        if arch_blocked:
            hold_msg = (
                f"awaiting GPU with compute capability >= {min_compute_capability}: "
                "no live runner has a GPU whose architecture can run this image "
                "(its GPUs report a lower CUDA compute capability)."
            )
            ctx = dict(task.context or {})
            changed = ctx.pop("runner_target_id", None) is not None
            if not ctx.get(CONTAINER_RUNNER_HOLD_CONTEXT_KEY):
                ctx[CONTAINER_RUNNER_HOLD_CONTEXT_KEY] = _utcnow().isoformat()
                changed = True
            if task.latest_status_message != hold_msg:
                task.latest_status_message = hold_msg
                changed = True
            if changed:
                task.context = ctx
                session.add(task)
                await session.commit()
            logger.info(
                "[RUNNERS] Holding compute_experiment %s: min_compute_capability "
                "%.1f declared but no GPU-arch-compatible runner available",
                task.id,
                min_compute_capability,
            )
            return "held"

    # ── Disk-fit hold: image declared + at least one container-capable runner
    # exists but every such runner reports too little free disk to fit the
    # multi-GB pull → keep QUEUED with an honest "waiting for disk headroom"
    # reason instead of dispatching to a box where the pull would die
    # ("no space left on device") and waste the claim. Placed BEFORE the
    # container-image hold so a container-capable-but-full fleet reports the
    # accurate disk reason rather than the misleading "no container-capable
    # runner" one. Fires only when a container-capable runner exists whose only
    # failing gate is disk (so this is a genuine, recoverable blocker — the
    # operator frees space and the same fleet takes the job). Reuses the
    # container-runner hold stamp so the existing bounded-wait sweep covers it.
    if runner is None and container_image:
        project_runners = active_runners_cache.get(task.project_id) or []
        image_store_floor = _container_image_min_free_disk_bytes()
        workspace_floor = _container_workspace_min_free_disk_bytes()
        disk_blocked = any(
            _runner_has_container_capability(r)
            and not _runner_has_free_disk(
                r,
                image_store_floor,
                workspace_required_bytes=workspace_floor,
                container_image=container_image,
            )
            for r in project_runners
        )
        if disk_blocked:
            hold_msg = (
                "Waiting for a runner with enough capacity: job declares "
                f"container_image={container_image!r} (a multi-GB image pull) but "
                "every container-capable runner reports workspace or container "
                "image-store capacity below its reserve "
                f"(workspace={workspace_floor} bytes, "
                f"image_store={image_store_floor} bytes). "
                "Without separate image-store telemetry, the legacy workspace/disk "
                "measurement must clear the image-store floor. "
                "A low Docker image store triggers "
                "stale-image cleanup on the runner; the job will claim after both "
                "known filesystems clear their respective floors."
            )
            ctx = dict(task.context or {})
            changed = ctx.pop("runner_target_id", None) is not None
            if not ctx.get(CONTAINER_RUNNER_HOLD_CONTEXT_KEY):
                ctx[CONTAINER_RUNNER_HOLD_CONTEXT_KEY] = _utcnow().isoformat()
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
                "(%r) but no container-capable runner has enough free disk",
                task.id,
                container_image,
            )
            return "held"

    # ── GPU-in-container hold: image + accelerator declared and at least one live
    # runner HAS a container runtime but cannot expose a GPU INSIDE the container
    # (no CDI daemon and no verified nvidia docker hook) → keep QUEUED with an
    # HONEST reason. Placed BEFORE the generic container-image hold so such a fleet
    # is NOT told "no live runner has docker_usable capability" — that is false and
    # actively misleading: docker IS present, only GPU passthrough is missing, so
    # the operator would waste time reinstalling docker that is already there
    # instead of enabling CDI / the nvidia container runtime. Fires only when a
    # container-capable-but-not-GPU-in-container runner exists (the genuine,
    # recoverable blocker for this fleet); a fleet with NO container runtime at all
    # falls through to the generic hold below, and a fleet with no live runner at
    # all falls through to the cloud backend. Reuses the container-runner hold stamp
    # so the existing bounded-wait sweep covers it unchanged.
    if runner is None and _job_requires_gpu_in_container(
        accelerator, container_image, vram_gb
    ):
        project_runners = active_runners_cache.get(task.project_id) or []
        # Keep the proof-specific diagnosis scoped to runners that survived every
        # selection gate *before* passthrough proof. A proof on a CPU-only,
        # wrong-task-type, full, runtime-less, or disk-full runner cannot make
        # the generic hold more truthful: it is not a runner this job could use
        # even if its proof were valid. This deliberately mirrors the ordering in
        # _select_eligible_runner; the passthrough and later VRAM/architecture
        # gates remain outside this subset because this branch diagnoses the
        # missing passthrough prerequisite specifically.
        image_store_floor = _container_image_min_free_disk_bytes()
        workspace_floor = _container_workspace_min_free_disk_bytes()
        pre_passthrough_eligible = [
            r
            for r in project_runners
            if _runner_handles_task_type(r, "compute_experiment")
            and not _runner_at_capacity(
                r, runner_pins_this_cycle.get(r.id, 0), "compute_experiment"
            )
            and (not accelerator or _runner_gpu_capable(r))
            and _runner_has_container_capability(r)
            and _runner_has_free_disk(
                r,
                image_store_floor,
                workspace_required_bytes=workspace_floor,
                container_image=container_image,
            )
        ]
        # Capacity is deliberately excluded from this companion subset. A full
        # runner with functional proof demonstrates that the fleet CAN satisfy
        # the runtime requirement; the accurate wait reason is capacity, not a
        # false claim that no selected runtime has proof. Task type, host GPU,
        # runtime and disk gates remain required, since proof on a runner that
        # fails one of those can never satisfy this job.
        pre_passthrough_capable = [
            r
            for r in project_runners
            if _runner_handles_task_type(r, "compute_experiment")
            and (not accelerator or _runner_gpu_capable(r))
            and _runner_has_container_capability(r)
            and _runner_has_free_disk(
                r,
                image_store_floor,
                workspace_required_bytes=workspace_floor,
                container_image=container_image,
            )
        ]
        gpu_in_container_blocked = any(
            not _runner_gpu_in_container_capable(r)
            for r in pre_passthrough_eligible
        )
        fleet_has_verified_proof = any(
            _runner_gpu_in_container_capable(r) for r in pre_passthrough_capable
        )
        if gpu_in_container_blocked and not fleet_has_verified_proof:
            blockers = sorted({
                _runner_gpu_passthrough_blocker(r)
                for r in pre_passthrough_eligible
                if not _runner_gpu_in_container_capable(r)
            })
            gpu_req = "an accelerator" if accelerator else f"vram_gb={vram_gb!r}"
            hold_msg = (
                "Waiting for a runner that can expose a GPU inside the container: "
                f"job declares both container_image={container_image!r} and "
                f"{gpu_req}, but no selected runtime has functional passthrough "
                f"proof ({'; '.join(blockers[:3])})."
            )
            ctx = dict(task.context or {})
            changed = ctx.pop("runner_target_id", None) is not None
            if not ctx.get(CONTAINER_RUNNER_HOLD_CONTEXT_KEY):
                ctx[CONTAINER_RUNNER_HOLD_CONTEXT_KEY] = _utcnow().isoformat()
                changed = True
            if task.latest_status_message != hold_msg:
                task.latest_status_message = hold_msg
                changed = True
            if changed:
                task.context = ctx
                session.add(task)
                await session.commit()
            logger.info(
                "[RUNNERS] Holding compute_experiment %s: container_image (%r) + "
                "GPU requirement (%s) declared but no live runner has "
                "GPU-in-container passthrough (docker present, CDI/nvidia-hook "
                "missing)",
                task.id,
                container_image,
                gpu_req,
            )
            return "held"

    # ── Legacy enforce-local hold: require_local + no runner → no cloud ─────────
    # Keep this after the proof-specific hold above: a local-only GPU image job
    # whose live runner fails functional passthrough needs the actionable runtime
    # diagnosis, not the generic "no live eligible runner" message.
    if runner is None and require_local:
        ctx = dict(task.context or {})
        changed = ctx.pop("runner_target_id", None) is not None  # drop any stale pin
        if not ctx.get(LOCAL_RUNNER_HOLD_CONTEXT_KEY):
            ctx[LOCAL_RUNNER_HOLD_CONTEXT_KEY] = _utcnow().isoformat()
            changed = True
        hold_msg = "Waiting for a live eligible local runner."
        if task.latest_status_message != hold_msg:
            task.latest_status_message = hold_msg
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

    # ── Container-image hold: image declared + registered runners but none is
    # currently eligible → keep QUEUED with an honest reason, never cloud-dispatch.
    # Distinguish a fleet with no runtime from one whose runtime-equipped runners
    # are blocked by capacity or another gate; never claim Docker is absent when
    # verified passthrough exists on a temporarily ineligible runner.
    # Only fires when the project has at least one live runner (runners exist but
    # lack the runtime); when no runners are registered at all we fall through to
    # the cloud backend so the job is not silently wedged.
    if runner is None and container_image:
        project_runners = active_runners_cache.get(task.project_id) or []
        if project_runners:
            if any(_runner_has_container_capability(r) for r in project_runners):
                hold_msg = (
                    "Waiting for an eligible container-capable runner: job declares "
                    f"container_image={container_image!r}; a container runtime is "
                    "present, but every such runner is at capacity or fails another "
                    "job requirement."
                )
            else:
                hold_msg = (
                    "Waiting for a container-capable runner: job declares "
                    f"container_image={container_image!r} but no live runner "
                    "has docker_usable capability."
                )
            ctx = dict(task.context or {})
            changed = ctx.pop("runner_target_id", None) is not None
            if not ctx.get(CONTAINER_RUNNER_HOLD_CONTEXT_KEY):
                ctx[CONTAINER_RUNNER_HOLD_CONTEXT_KEY] = _utcnow().isoformat()
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
        # Successful dispatch is the ONLY place a compute runner-hold stamp clears.
        # Clear every key in the aggregate so a task that was held under one hold
        # class and dispatches under another cannot carry a stale stamp into the
        # bounded hold-timeout sweep (which would terminal-fail a task that is
        # already running on a runner).
        for _hold_key in COMPUTE_RUNNER_HOLD_CONTEXT_KEYS:
            ctx.pop(_hold_key, None)
        _clear_execution_placement_wait(ctx)
        # A strict-pin hold that is now dispatching must drop its stale hold
        # reason — the pinned runner became eligible.
        ctx.pop("runner_pin_hold", None)
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
        container_image_override=resolved_default_container_image,
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
        # The project's compute-job concurrency cap is TRANSIENT back-pressure, not a
        # launch failure: active jobs only shrink absent new launches, so this exact
        # task launches unchanged once a running job goes terminal. Nothing upstream
        # defers it (the cap is enforced nowhere else in dispatch — the task-level
        # per-project cap counts active Tasks, and compute_experiments PARK after
        # launch rather than staying active), so the N+1th experiment in a capped
        # project reliably reached create_compute_job and was PERMANENTLY FAILED —
        # burning the direction's failure budget on the orchestrator's own queueing
        # decision and feeding the churn guardrail phantom failures. Defer instead.
        if detail.get("error") == "compute_job_concurrency_limit_exceeded":
            return await _defer_compute_experiment_task(session, task, msg)
        # require_local_runner with no runner-pinned backend is an infrastructure
        # AVAILABILITY condition, not misconfig — and this same function already HOLDS
        # for exactly that condition above (the runner-is-None + require_local branch),
        # so terminal-failing it here would contradict the established contract. Reached
        # only if that pre-check and this chokepoint disagree (e.g. the selected runner
        # lost its pin under the re-lock). Take the hold: the bounded 6h sweep
        # (_fail_stale_local_runner_holds) still terminal-fails a hold nothing resolves,
        # so this cannot wedge.
        if detail.get("error") == "local_runner_required":
            ctx = dict(task.context or {})
            if not ctx.get(LOCAL_RUNNER_HOLD_CONTEXT_KEY):
                ctx[LOCAL_RUNNER_HOLD_CONTEXT_KEY] = _utcnow().isoformat()
            ctx.pop("runner_target_id", None)
            task.context = ctx
            task.latest_status_message = msg[:500]
            session.add(task)
            _held_task_id = str(task.id)
            await session.commit()
            logger.info(
                "[ORCHESTRATOR] compute_experiment %s HELD on require-local launch "
                "chokepoint (no runner-pinned backend): %s",
                _held_task_id,
                msg[:200],
            )
            return "held"
        # Everything else is a genuine, non-self-clearing launch failure — spend
        # ceiling (needs an operator to raise the budget; the rolling window only
        # decays over DAYS, not dispatch cycles), spend-approval gate, hardware-policy
        # violation, bad checkpoint-resume reference. Label them ``compute_launch_failed``
        # exactly as the agent path did. Using ``BLOCKED_PRECONDITION`` on a terminal
        # FAILED task would make a parent waiting on this child mis-read it as still in
        # flight (blocked_task_unblock_sweeps treats failure_category
        # BLOCKED_PRECONDITION as "waiting") and wedge.
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

    # The launch succeeded, so any transient-deferral breadcrumbs from earlier cycles
    # are now stale — drop them (including the escalation counter, so a LATER cap hit
    # on this task restarts the backoff at the base delay rather than inheriting a
    # maxed-out one). Rides the park_task commit below; `retry_after_until` itself was
    # already cleared by the candidate scan when the backoff lapsed.
    if (task.context or {}).get(COMPUTE_LAUNCH_DEFERRED_REASON_CONTEXT_KEY) or (
        task.context or {}
    ).get(COMPUTE_LAUNCH_DEFER_COUNT_CONTEXT_KEY):
        _launched_ctx = dict(task.context or {})
        _launched_ctx.pop(COMPUTE_LAUNCH_DEFERRED_REASON_CONTEXT_KEY, None)
        _launched_ctx.pop(COMPUTE_LAUNCH_DEFER_COUNT_CONTEXT_KEY, None)
        task.context = _launched_ctx
        session.add(task)

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
