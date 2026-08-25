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

import re
import uuid
from typing import NamedTuple

from app.models import LocalRunner
from app.task_capabilities import get_task_capability


_RUFF_COMMAND_RE = re.compile(r"^(?:python3?\s+-m\s+)?ruff(?:\s|$)")
_PYTEST_COMMAND_RE = re.compile(r"^(?:python3?\s+-m\s+)?pytest(?:\s|$)")
_PROJECT_VERIFICATION_HARNESS_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
_PROJECT_VERIFICATION_VERSION = 1


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
    source_sha: str | None = None
    agent_source_sha: str | None = None


def _effective_build_command(entry: dict, kind: str) -> str:
    confirmed = entry.get(f"confirmed_{kind}_cmd")
    if entry.get("commands_stale") is not True and isinstance(confirmed, str):
        confirmed = confirmed.strip()
        if confirmed:
            return confirmed
    detected = entry.get(f"{kind}_cmd")
    return detected.strip() if isinstance(detected, str) else ""


def _project_backend_verification_directories(build_config: object) -> tuple[str, ...]:
    if not isinstance(build_config, dict):
        return ()
    directories = build_config.get("directories")
    if not isinstance(directories, list):
        return ()
    verified: list[str] = []
    for entry in directories:
        if not isinstance(entry, dict) or entry.get("type") != "python":
            continue
        lint_cmd = _effective_build_command(entry, "lint")
        test_cmd = _effective_build_command(entry, "test")
        project_verification = entry.get("project_verification")
        path = entry.get("path")
        if (
            isinstance(path, str)
            and path.strip()
            and _RUFF_COMMAND_RE.match(lint_cmd)
            and _PYTEST_COMMAND_RE.match(test_cmd)
            and isinstance(project_verification, dict)
            and project_verification.get("protocol") == "blueprint-v1"
            and isinstance(project_verification.get("harness"), str)
            and bool(project_verification["harness"].strip())
            and not project_verification["harness"].startswith("/")
            and ".." not in project_verification["harness"]
            and _PROJECT_VERIFICATION_HARNESS_RE.fullmatch(
                project_verification["harness"]
            )
        ):
            normalized = path.strip().rstrip("/") or "."
            if normalized not in verified:
                verified.append(normalized)
    return tuple(verified)


def _task_is_authoring(task_type: str) -> bool:
    capability = get_task_capability(task_type)
    return bool(
        capability is not None
        and capability.agent_mode == "write"
        and capability.run_shape == "workflow"
        and capability.output_kind == "pr"
    )


def _task_requires_project_verification(
    task_type: str, build_config: object
) -> bool:
    return bool(
        _task_is_authoring(task_type)
        and _project_backend_verification_directories(build_config)
    )


def _successful_verification_probe(probes: dict, name: str) -> bool:
    probe = probes.get(name)
    return bool(
        isinstance(probe, dict)
        and probe.get("ok") is True
        and type(probe.get("exit_code")) is int
        and probe["exit_code"] == 0
    )


def _runner_has_project_verification(runner: LocalRunner | RunnerDispatchSnapshot) -> bool:
    """Validate the complete v1 functional report, failing closed on every gap."""
    report = (runner.capabilities or {}).get("project_verification")
    if not isinstance(report, dict):
        return False
    boundary = report.get("boundary")
    probes = report.get("probes")
    if not isinstance(boundary, dict) or not isinstance(probes, dict):
        return False
    version = report.get("version")
    if (
        type(version) is not int
        or version != _PROJECT_VERIFICATION_VERSION
        or report.get("ready") is not True
        or boundary.get("class") != "agent"
        or boundary.get("backend") != "bwrap"
    ):
        return False
    return all(
        _successful_verification_probe(probes, name)
        for name in ("ruff", "docker_postgres")
    )


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


def _runner_accepting_new_work(
    runner: LocalRunner | RunnerDispatchSnapshot,
) -> bool:
    """False while a version-mismatched daemon drains before auto-update."""
    return (runner.capabilities or {}).get("update_draining") is not True


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
}


def _selected_container_runtime_and_mode(
    caps: dict,
) -> tuple[str | None, str | None]:
    """Return the runtime and GPU flag mode the runner will actually execute."""
    backends = caps.get("isolation_backends")
    if not isinstance(backends, dict):
        return None, None
    if backends.get("docker_usable"):
        return "docker", "cdi" if backends.get("docker_cdi_gpu") else "hook"
    if backends.get("podman_usable"):
        return "podman", "hook"
    if backends.get("apptainer_ok"):
        return "apptainer", "nv"
    return None, None


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


def _runner_chromium_usable(runner: LocalRunner | RunnerDispatchSnapshot) -> bool:
    """Whether a runner can serve Playwright Chromium to agent sandboxes.

    Reads the NESTED inventory the runner heartbeat writes
    (``capabilities_inventory`` in ``scripts/runner_isolation.py``). Viewport
    verification needs both a reachable Docker daemon and a Playwright-managed
    Chromium cache; either missing leg is unknown/not capable. The TOP-LEVEL
    ``playwright_chromium_usable`` key carries a different live-probe contract
    and is deliberately ignored.
    """
    caps = runner.capabilities or {}
    backends = caps.get("isolation_backends")
    if not isinstance(backends, dict):
        return False
    return (
        backends.get("docker_usable") is True
        and backends.get("playwright_chromium_usable") is True
    )


def _runner_python_runtime_incapable(
    runner: LocalRunner | RunnerDispatchSnapshot,
) -> bool:
    """True only when the runner PROVED it cannot build a compute interpreter.

    Deliberately an EXCLUSION predicate rather than a capability one, and the
    asymmetry is the point. An imageless (bwrap) compute job gets its Python
    from the RUNNER — ``scripts/local_runner.py`` guarantees an interpreter with
    a working pip only on that path, because a container image ships its own —
    and the runner publishes the outcome of its own provisioning attempt. Only
    that proof excludes a host:

      * nested report absent          -> unknown      -> NOT excluded
      * report present, usable True   -> capable      -> NOT excluded
      * report present, usable None   -> unreadable   -> NOT excluded
      * report present, usable False  -> PROVEN unfit -> excluded

    The flat ``python_runtime_usable`` bool is intentionally NOT read. The
    runner derives it as ``bool((report or {}).get("usable"))``, so a host whose
    boot-time provisioner thread has not published yet reports ``False`` while
    being perfectly capable. Gating on that bit would empty the candidate pool
    of a healthy fleet at exactly the moment it restarts — the failure mode a
    capability gate must never have.
    """
    caps = runner.capabilities or {}
    report = caps.get("python_runtime")
    if not isinstance(report, dict):
        return False
    return report.get("usable") is False


# A card's marketing size overstates what nvidia-smi reports: driver/ECC
# reserves shave 1–2% off the visible total (a "16 GB" V100-SXM2 reports
# 16145 MiB, not 16384). vram_gb requirements are authored in marketing GB,
# so without slack a job naming exactly the card it needs (vram_gb=16 on a
# 16 GB card) is PERMANENTLY undispatchable. 3% covers observed reserve
# overhead without admitting a meaningfully busy GPU.
_VRAM_REQUIREMENT_TOLERANCE = 0.97


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

    The comparison allows ``_VRAM_REQUIREMENT_TOLERANCE`` slack so a
    marketing-GB requirement matches the card class it names despite the
    driver/ECC reserve gap.
    """
    caps = runner.capabilities or {}
    free_mib_list = caps.get("gpu_free_vram_mib")
    if not isinstance(free_mib_list, list) or not free_mib_list:
        return True
    effective_floor = required_mib * _VRAM_REQUIREMENT_TOLERANCE
    return any(mib >= effective_floor for mib in free_mib_list)


def _known_free_bytes(value: object) -> int | None:
    """Return trusted non-negative capacity telemetry, else ``None``.

    ``None`` deliberately means unknown rather than zero. Older runners do not
    advertise the separate workspace/image-store fields, and dispatch must not
    strand their work solely because of that protocol-version gap.
    """
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _runner_container_image_storage(
    runner: LocalRunner | RunnerDispatchSnapshot,
) -> dict[str, int | str | None]:
    """Normalized storage telemetry relevant to a container-image pull.

    ``disk_free_bytes`` remains the legacy workspace measurement. New runners
    also send ``workspace_free_bytes`` and the filesystem that backs Docker or
    Podman's image store. Both known capacities must clear the hard floor: a
    pull needs the image store, while checkout/artifact work still needs the
    workspace. Unknown values intentionally preserve legacy eligibility.
    """
    caps = runner.capabilities or {}
    workspace = _known_free_bytes(caps.get("workspace_free_bytes"))
    if workspace is None:
        workspace = _known_free_bytes(caps.get("disk_free_bytes"))
    image_store = _known_free_bytes(caps.get("container_image_store_free_bytes"))
    runtime = caps.get("container_image_store_runtime")
    path = caps.get("container_image_store_path")
    return {
        "workspace_free_bytes": workspace,
        "image_store_free_bytes": image_store,
        "image_store_runtime": runtime if isinstance(runtime, str) else None,
        "image_store_path": path if isinstance(path, str) else None,
    }


def _normalized_container_image_ref(ref: object) -> str | None:
    """Canonical form of a container image reference for presence comparison.

    ``docker image ls`` reports Docker Hub refs WITHOUT the implicit
    ``docker.io/library/`` prefix (``python:3.12-slim``) while refs on an
    explicit registry (``nvcr.io/nvidia/tensorflow:24.12-tf2-py3``) keep their
    full form. Strip the implicit Hub prefixes and default the ``:latest`` tag
    so a job naming either spelling matches the runner's reported store
    contents. Digest refs (``@sha256:...``) compare verbatim.
    """
    if not isinstance(ref, str):
        return None
    normalized = ref.strip()
    if not normalized:
        return None
    for prefix in ("docker.io/library/", "docker.io/", "library/"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    if "@" not in normalized:
        last_segment = normalized.rsplit("/", 1)[-1]
        if ":" not in last_segment:
            normalized = f"{normalized}:latest"
    return normalized


def _runner_container_image_present(
    runner: LocalRunner | RunnerDispatchSnapshot,
    container_image: str | None,
) -> bool:
    """Whether the runner's heartbeat says *container_image* is already local.

    Reads the ``container_images_present`` capability list the runner reports
    from its own image store. Absence of the field means UNKNOWN (older
    runner, or a failed probe) and returns False — callers then apply the full
    image-pull disk floor, exactly the historical behavior. Only a positive,
    normalized-exact match ever waives anything.
    """
    requested = _normalized_container_image_ref(container_image)
    if requested is None:
        return False
    caps = runner.capabilities or {}
    present = caps.get("container_images_present")
    if not isinstance(present, list):
        return False
    return any(
        _normalized_container_image_ref(entry) == requested for entry in present
    )


def _runner_has_free_disk(
    runner: LocalRunner | RunnerDispatchSnapshot,
    image_store_required_bytes: int,
    *,
    workspace_required_bytes: int | None = None,
    container_image: str | None = None,
) -> bool:
    """Whether a container-image job fits each known required filesystem.

    A container image is written to its runtime data-root, which can differ
    from the runner workspace. Pulling the image needs a multi-GB image-store
    reserve, while checkout/artifact scratch only needs the smaller workspace
    reserve. Applying the image-pull floor to both filesystems unnecessarily
    strands runners whose container store lives on a large separate volume.

    When ``container_image`` is given and the runner reports that exact image
    in its local store (``container_images_present``), the image-pull floor is
    waived entirely — the guarded multi-GB pull is a no-op — while the
    workspace scratch floor still applies. A runner that does not report the
    capability keeps the full floor (absence means unknown, never "cached").

    ``workspace_required_bytes=None`` preserves the historical equal-floor
    behavior for direct callers. Production container dispatch passes both
    floors explicitly. Missing/malformed telemetry remains backward-compatible
    eligible, matching the other runner-capability predicates.
    """
    image_floor = max(0, image_store_required_bytes)
    if image_floor > 0 and _runner_container_image_present(
        runner, container_image
    ):
        # No pull will happen: the requested image is already in the local
        # store. Zeroing the floor here also releases the legacy single-volume
        # escalation in _runner_effective_workspace_disk_floor, so a dgx-class
        # runner permanently below the pull floor can still run its cached
        # image (the workspace floor below remains fully enforced).
        image_floor = 0
    workspace_floor = (
        image_floor
        if workspace_required_bytes is None
        else max(0, workspace_required_bytes)
    )
    if image_floor <= 0 and workspace_floor <= 0:
        return True
    storage = _runner_container_image_storage(runner)
    image_store_free = storage["image_store_free_bytes"]
    effective_workspace_floor = _runner_effective_workspace_disk_floor(
        runner,
        image_floor,
        workspace_required_bytes=workspace_floor,
    )
    known_with_floors = (
        (storage["workspace_free_bytes"], effective_workspace_floor),
        (image_store_free, image_floor),
    )
    return all(
        value is None or value >= floor
        for value, floor in known_with_floors
    )


def _runner_effective_workspace_disk_floor(
    runner: LocalRunner | RunnerDispatchSnapshot,
    image_store_required_bytes: int,
    *,
    workspace_required_bytes: int,
) -> int:
    """Workspace floor after accounting for legacy single-volume telemetry."""
    image_floor = max(0, image_store_required_bytes)
    workspace_floor = max(0, workspace_required_bytes)
    storage = _runner_container_image_storage(runner)
    # Without separate image-store telemetry, the legacy workspace/disk field
    # may also back the container runtime. Preserve the historical full pull
    # floor on that single known filesystem instead of assuming it is split.
    if storage["image_store_free_bytes"] is None:
        return max(workspace_floor, image_floor)
    return workspace_floor


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

    Capability is earned only by the runner's bounded functional probe through
    the exact selected runtime and GPU flag mode. Runtime presence, CDI config,
    and a host GPU are inputs to a probe, never substitutes for its result. Old
    runners omit the additive proof and therefore fail closed for image+GPU jobs
    while remaining eligible for CPU image jobs and imageless GPU jobs.
    """
    return _container_gpu_passthrough_verified(runner.capabilities or {})


def _container_gpu_passthrough_verified(caps: dict) -> bool:
    """Whether capabilities prove passthrough for the selected runtime/mode."""
    proof = caps.get("container_gpu_passthrough")
    if not isinstance(proof, dict):
        return False
    selected, expected_mode = _selected_container_runtime_and_mode(caps)
    report = proof.get(selected) if selected else None
    return (
        isinstance(report, dict)
        and report.get("verified") is True
        and report.get("mode") == expected_mode
    )


def _runner_gpu_passthrough_blocker(
    runner: LocalRunner | RunnerDispatchSnapshot,
) -> str:
    """Bounded, operator-facing blocker from runner-produced probe evidence."""
    caps = runner.capabilities or {}
    proof = caps.get("container_gpu_passthrough")
    if not isinstance(proof, dict) or not proof:
        return "runner has not reported a verified GPU passthrough probe"
    selected, expected_mode = _selected_container_runtime_and_mode(caps)
    report = proof.get(selected) if selected else None
    if not isinstance(report, dict):
        return f"selected runtime {selected or 'unknown'} has no probe report"
    mode = report.get("mode") or "unknown mode"
    if mode != expected_mode:
        return (
            f"{selected} probe mode {mode!r} does not match "
            f"selected mode {expected_mode!r}"
        )
    reason = report.get("reason") or "probe failed"
    return f"{selected} {mode} probe: {reason}"


def _job_requires_gpu_in_container(
    accelerator: str | None,
    container_image: str | None,
    vram_gb: float | None,
) -> bool:
    """Whether a job's declaration requires the runner to expose a GPU INSIDE the
    container — the trigger condition for the :func:`_runner_gpu_in_container_capable`
    gate.

    True only when a ``container_image`` is declared AND the job asks for a GPU. A
    GPU is asked for by EITHER an explicit ``accelerator`` string OR a positive
    ``vram_gb`` GPU-memory floor: VRAM exists only on a GPU, so an imaged job that
    reserves VRAM but omits an ``accelerator`` string still needs GPU passthrough
    into the container and would otherwise fail at runtime with "no supported GPU
    detected". This mirrors :func:`_runner_gpu_in_container_capable`'s own contract
    ("container_image AND ``accelerator``/``vram_gb``") — keeping the DISPATCH
    trigger and the CAPABILITY predicate aligned so the two never drift.

    A non-positive / absent ``vram_gb`` is no GPU request on its own (matching the
    ``vram_gb is not None and vram_gb > 0`` VRAM-fit gate in
    ``_select_eligible_runner``), so a plain CPU imaged job is unaffected.
    """
    if not container_image:
        return False
    return bool(accelerator) or (vram_gb is not None and vram_gb > 0)


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
