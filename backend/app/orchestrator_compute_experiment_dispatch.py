"""Direct compute-experiment dispatch — QUEUED task to a running ComputeJob.

Extracted from :mod:`app.orchestrator_runner_dispatch`, which is a chronic
size-limit offender (decomposition required; cap bumps are forbidden by
``app.size_limit_policy``).

This is the ``compute_experiment`` half of the dispatch cluster: it validates
the agent-authored payload, resolves the container image and GPU/VRAM fit,
picks a runner (or the cloud backend), creates the ComputeJob, and PARKs the
task. It also owns the two non-terminal outcomes that keep a compute task
QUEUED honestly rather than failing it — the hold markers stamped when no
runner can take it, and the transient cap DEFERRAL with its escalating
backoff and saturation alert.

The hold-marker constants live here because this is the only path that stamps
them; ``orchestrator_runner_dispatch._fail_stale_local_runner_holds`` imports
them back to sweep them, upholding the stamped-implies-swept invariant
documented on ``COMPUTE_RUNNER_HOLD_CONTEXT_KEYS``.

Everything below is lifted verbatim (behaviour-preserving) and re-exported
from ``orchestrator_runner_dispatch`` (and thence from ``app.orchestrator``)
so existing import paths keep working unchanged.
"""

import logging
import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import LocalRunner, Project, Task, TaskStatus
from app.orchestrator_runner_placement import (
    _clear_execution_placement_wait,
    _hold_task_for_local_only_placement,
)
from app.orchestrator_runner_selection import (
    FORCED_RUNNER_TARGET_CONTEXT_KEY,
    _diagnose_strict_runner_pin_hold,
    _lookup_runner_hostname,
    _num_or_none,
    _payload_runner_target,
    _pin_hold_stable_key,
    _select_eligible_runner,
)
from app.resource_envelope import resolve_project_require_local_runner
from app.runner_capability_contracts import (
    _coerce_min_compute_capability,
    _coerce_vram_gb,
    _container_image_min_free_disk_bytes,
    _container_workspace_min_free_disk_bytes,
)
from app.runner_decline import (
    decline_exclusion_is_advisory,
    decline_record_for_runner,
    recently_declined_runner_ids,
)
from app.runner_eligibility import (
    RunnerDispatchSnapshot,
    _job_requires_gpu_in_container,
    _runner_at_capacity,
    _runner_gpu_capable,
    _runner_gpu_in_container_capable,
    _runner_gpu_passthrough_blocker,
    _runner_handles_task_type,
    _runner_has_container_capability,
    _runner_has_free_disk,
    _runner_meets_min_compute_capability,
)
from app.strategic_runtime_work import (
    CONTAINER_RUNNER_HOLD_CONTEXT_KEY,
    LOCAL_RUNNER_HOLD_CONTEXT_KEY,
)
from app.time_utils import _utcnow

logger = logging.getLogger("blueprint")


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


def _image_provenance(container_image: str, payload_declared: bool) -> str:
    """Describe the held job's image AND where it came from.

    A job can now reach the container-capable-runner gate without having asked
    for a container: when the payload declares no image, dispatch supplies the
    project's declared one. Saying "job declares container_image=..." over the
    top of that sends an operator to read a payload that mentions no image at
    all, so the two cases have to read differently.
    """
    if payload_declared:
        return f"job declares container_image={container_image!r}"
    return (
        f"the project environment contract supplies container_image="
        f"{container_image!r} (the payload declared none)"
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
    #
    # NOT gated on `accelerator`. It used to be, and that gate was the reason the
    # environment contract did not exist in practice: the large majority of
    # compute_experiment jobs declare no accelerator, so they skipped image
    # resolution entirely and ran as a bare `bash -lc` against whatever toolchain
    # the claiming host happened to have. That is what produced the commonest
    # terminal log in the corpus -- `no working pip found (tried "-m pip" and
    # PATH) - cannot provision the runtime` -- along with the exit-127
    # (command not found) and exit-126 (not executable) failures. A project that
    # declares an image means it for all of its work, not only its GPU work.
    #
    # The gate existed to dodge a downstream defect, not to express a policy:
    # attaching an image armed the runner's GPU-arch pre-flight (which fires on
    # `assigned_gpus and (accelerator_type or container_image)`, and
    # `assigned_gpus` comes from the HOST having GPUs, not from the job wanting
    # them), so CPU-only work landing on a GPU host died on an arch mismatch it
    # had no stake in. That is fixed at the root in `local_runner.py`: a job that
    # declared no accelerator now RELEASES the unusable device and re-plans
    # CPU-only instead of aborting. With the kill gone the workaround is not
    # needed, and the contract can apply to every job as intended.
    resolved_default_container_image: str | None = None
    payload_declared_image = bool(container_image)
    if not container_image:
        from app.compute_backends import resolve_project_baked_image_strict

        project = await session.get(Project, task.project_id)
        resolved_default_container_image, image_error = (
            await resolve_project_baked_image_strict(session, project)
        )
        if image_error is not None:
            # Fail CLOSED only for a payload that actually asked for an
            # accelerator. Those jobs need the declared image to mean anything,
            # and running them on the host toolchain is a reproducibility
            # failure the operator must see.
            #
            # A BARE payload degrades to image-less instead. The commonest
            # reason a declared image does not resolve is that it has not been
            # built YET, and that window is transient project state -- letting
            # it terminal-fail every CPU-only job in the project would turn a
            # pending build into an outage for work that never asked for an
            # image. Applying the contract where it resolves and degrading
            # where it does not is what makes it safe to apply the contract to
            # every job at all.
            if accelerator:
                return await _fail_compute_experiment_task(
                    session,
                    task,
                    "baked_image_unresolvable",
                    image_error,
                )
            logger.warning(
                "[RUNNERS] compute_experiment %s: project image unresolvable "
                "(%s) — the payload declared no accelerator, so it launches "
                "image-less rather than failing on a contract it never invoked",
                task.id,
                image_error,
            )
            resolved_default_container_image = None
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
                else decline_record_for_runner(
                    task,
                    pin_runner_id,
                    runner=next(
                        (
                            candidate
                            for candidate in active_runners_cache.get(
                                task.project_id, []
                            )
                            if str(candidate.id) == pin_runner_id
                        ),
                        None,
                    ),
                )
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
                "Waiting for a runner with enough capacity: "
                f"{_image_provenance(container_image, payload_declared_image)} "
                "(a multi-GB image pull) but "
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
                f"job runs under container_image={container_image!r} with "
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
                    "Waiting for an eligible container-capable runner: "
                    f"{_image_provenance(container_image, payload_declared_image)}; "
                    "a container runtime is present, but every such runner is at "
                    "capacity or fails another job requirement."
                )
            else:
                hold_msg = (
                    "Waiting for a container-capable runner: "
                    f"{_image_provenance(container_image, payload_declared_image)} "
                    "but no live runner has docker_usable capability."
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
