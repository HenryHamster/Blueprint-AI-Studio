"""Runner SELECTION — pick the one runner that should take this task.

Extracted from :mod:`app.orchestrator_runner_dispatch`, which is a chronic
size-limit offender (decomposition required; cap bumps are forbidden by
``app.size_limit_policy``).

:mod:`app.runner_eligibility` holds the per-runner predicates; this module
holds the two consumers that must agree on them exactly:

* :func:`_select_eligible_runner` — filter the candidate pool and choose a
  winner (least-loaded, degraded-last, deterministic tiebreak).
* :func:`_diagnose_strict_runner_pin_hold` — explain, for a STRICT pin, why
  the pinned runner was skipped this cycle.

When those two drift, a task is held with a reason that does not match the
reason it was actually skipped, so they live side by side and share one
eligibility rule. The operator-pin plumbing they both read
(``FORCED_RUNNER_TARGET_CONTEXT_KEY``, ``_payload_runner_target``,
``_pin_hold_stable_key``) and the hostname lookup used to render pin holds
belong to the same seam.

Re-exported from ``orchestrator_runner_dispatch`` (and thence from
``app.orchestrator``) so existing import paths keep working unchanged.
"""

import logging
import re
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LocalRunner
from app.runner_capability_contracts import (
    _container_image_min_free_disk_bytes,
    _container_workspace_min_free_disk_bytes,
)
from app.runner_decline import (
    decline_exclusion_applies_to_runner,
    decline_exclusion_is_advisory_for_candidates,
)
from app.runner_eligibility import (
    RunnerDispatchSnapshot,
    _job_requires_gpu_in_container,
    _runner_accepting_new_work,
    _runner_active_compute_job_count,
    _runner_active_task_count,
    _runner_agent_available,
    _runner_at_capacity,
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
)
from app.runner_health_monitors import (
    _RUNNER_DEGRADED_FAILURE_THRESHOLD,
    _RUNNER_LIVENESS_SECONDS,
)
from app.runner_project_binding import allowed_project_ids

logger = logging.getLogger("blueprint")


# task.context key holding an operator's HARD runner pin for a compute_experiment.
# Kept deliberately SEPARATE from the soft ``preferred_runner_target_id`` affinity
# hint: a forced target NEVER silently falls back to a different runner or to
# cloud — when it is not eligible this cycle the task is HELD QUEUED. The soft
# preferred hint keeps its fallback behaviour untouched.
FORCED_RUNNER_TARGET_CONTEXT_KEY = "forced_runner_target_id"

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
            [
                r
                for r in candidates
                if not decline_exclusion_applies_to_runner(declined_runner_ids, r)
            ]
        )
        epoch_advisory = decline_exclusion_is_advisory_for_candidates(
            declined_runner_ids, task_type, candidates
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
        if not eligible and (declined_exclusion_advisory or epoch_advisory):
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
