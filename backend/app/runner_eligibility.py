"""Runner eligibility predicates — can THIS runner take THIS piece of work?

Extracted from :mod:`app.orchestrator_runner_dispatch`, which is a chronic
size-limit offender (decomposition required, cap bumps forbidden by
``app.size_limit_policy``). These are the pure, session-free predicates over a
runner's advertised capabilities and its per-cycle load: task-type coverage,
agent availability, agent/compute capacity, verified GPU capability, container
runtime, and free VRAM.

They are pure functions of a :class:`RunnerDispatchSnapshot` (or a bare
``LocalRunner``) plus an in-cycle pin count — no DB, no network — which is what
lets both the dispatcher (``_select_eligible_runner``) and the pin-hold
diagnostic (``_diagnose_strict_runner_pin_hold``) share ONE eligibility rule.
That sharing is the point: when the two drift, a task is held with a reason that
does not match the reason it was actually skipped.

Re-exported from ``orchestrator_runner_dispatch`` so existing import paths and
``orch._runner_*`` references keep working.
"""

import uuid
from typing import NamedTuple

from app.models import LocalRunner


class RunnerDispatchSnapshot(NamedTuple):
    """Session-independent view of a live local runner used during dispatch.

    ``project_id`` is a per-(runner, project) VIEW, not the runner's single home
    project: a multi-project runner appears once per project on its allow-list,
    each copy sharing the SAME ``capabilities`` dict. Because the unified
    per-runner load accounting (``_active_agent_task_count`` and
    ``active_compute_job_ids``, injected once by ``_load_active_runners_cache``)
    lives in that shared ``capabilities`` dict, every copy reports the SAME
    load — so a runner serving projects A and B is sized against one unified
    ``max_concurrent`` ledger and can never double-book across the two. The
    eligibility predicates below read only ``capabilities`` (never ``project_id``),
    so the pure-function contract is unchanged by the fan-out.
    """

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


def _runner_has_free_disk(
    runner: LocalRunner | RunnerDispatchSnapshot,
    required_bytes: int,
) -> bool:
    """Whether this runner has at least ``required_bytes`` of free disk.

    A container-image compute job must PULL a multi-GB image onto the runner
    before it can start. Sent to a nearly-full box the pull dies part-way
    ("no space left on device") and the claim is wasted — the job should have
    held for a runner (or cloud backend) that can fit it.

    Reads the heartbeat-reported ``disk_free_bytes`` (the free bytes on the
    runner's image/workspace filesystem, the same field the low-disk health
    sweep consults). As with :func:`_runner_has_free_vram`, absence of
    telemetry is NOT a disqualification: a runner that never reports
    ``disk_free_bytes`` (older runner) is treated as eligible — we gate
    dispatch only on real contradicting data, never on missing data, so a
    legitimately-configured runner is never demoted (backward compatible). A
    malformed value is likewise ignored. A non-positive floor is no floor.
    """
    if required_bytes <= 0:
        return True
    caps = runner.capabilities or {}
    disk_free = caps.get("disk_free_bytes")
    if disk_free is None:
        return True
    try:
        return int(disk_free) >= required_bytes
    except (TypeError, ValueError):
        # Malformed telemetry — do not gate on data we cannot trust.
        return True


def _runner_meets_min_compute_capability(
    runner: LocalRunner | RunnerDispatchSnapshot,
    min_cc: float,
) -> bool:
    """Whether any GPU on this runner meets an image's CUDA compute-capability floor.

    A container image is built for a specific set of GPU architectures (its bundled
    CUDA/torch wheels carry compiled SASS/PTX only for those). Sent to a box whose
    GPU is OLDER than the image's floor — an ``sm_80``-built image on a V100
    (``sm_70``) — the job launches and then dies inside the sandbox ("no kernel
    image is available for execution"). Today that is only caught REACTIVELY by the
    runner's in-sandbox probe, which fails the job terminally, and nothing stops the
    same doomed ``(runner, image)`` pairing from being re-selected and thrashing.

    Reads the heartbeat-reported ``gpu_compute_capabilities`` list (one CUDA compute
    capability float per GPU, index-aligned with ``gpu_names``: ``7.0`` for a V100,
    ``8.9`` for an L40S) and returns True when at least one GPU meets ``min_cc``.

    As with :func:`_runner_has_free_vram`, absence of telemetry is NOT a
    disqualification: a runner that never reports ``gpu_compute_capabilities``
    (older runner, or one that does not expose nvidia-smi) is treated as eligible —
    we gate dispatch only on real contradicting data, never on missing data, so a
    legitimately-configured runner is never demoted (backward compatible). The
    reactive in-sandbox probe remains the safety net for those. A non-positive floor
    is no floor at all.
    """
    if min_cc <= 0:
        return True
    caps = runner.capabilities or {}
    cc_list = caps.get("gpu_compute_capabilities")
    if not isinstance(cc_list, list) or not cc_list:
        return True
    try:
        return any(float(cc) >= min_cc for cc in cc_list)
    except (TypeError, ValueError):
        # Malformed telemetry entry — do not gate on data we cannot trust.
        return True


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


def _runner_gpu_in_container_capable(
    runner: LocalRunner | RunnerDispatchSnapshot,
) -> bool:
    """Whether a runner's container backend can pass GPUs through to containers.

    When a job declares BOTH ``container_image`` AND ``accelerator``/``vram_gb``,
    the runner must be able to expose GPUs *inside* the container — not merely
    have a container runtime and a GPU independently (today a runner with both
    can receive such a job and fail at runtime with "no supported GPU detected").

    * **Non-docker** containers (podman, apptainer, enroot) handle GPU passthrough
      natively — they are capable whenever the backend is available.
    * **Docker** requires either CDI GPU support (``docker_cdi_gpu``, heartbeated
      via ``isolation_backends``) or an nvidia-runtime hook (implied by a docker
      runner with verified GPU capability). Both paths can pass GPUs into the
      container (CDI via ``--device nvidia.com/gpu=N``, hook via ``--gpus``).

    Returns ``False`` when no container backend is available or when a docker-only
    runner lacks both CDI and GPU capability.
    """
    caps = runner.capabilities or {}
    backends = caps.get("isolation_backends")
    if not isinstance(backends, dict):
        return False

    # Non-docker containers handle GPU passthrough natively.
    non_docker_keys = _CONTAINER_BACKEND_KEYS - {"docker_usable"}
    if any(backends.get(k) for k in non_docker_keys):
        return True

    # Docker needs explicit GPU passthrough: CDI or nvidia-hook.
    if backends.get("docker_usable"):
        # CDI → explicit GPU-in-container support via --device nvidia.com/gpu=N.
        if backends.get("docker_cdi_gpu"):
            return True
        # No CDI but the runner has verified GPU capability → nvidia-hook
        # (--gpus device=... flag works).  Hook daemons return docker_cdi_gpu=False
        # but are equally capable of exposing GPUs inside containers.
        return bool(_runner_gpu_capable(runner))

    return False


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
