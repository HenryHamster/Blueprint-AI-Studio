"""Isolation core for the Blueprint self-hosted fleet runner.

The single invariant this module enforces: **every untrusted task — agent tasks
and compute jobs alike — runs inside an isolation boundary, or the runner
declines it.** There is no bare/unsandboxed execution path, no override, and no
"permissive" mode. If no boundary can be built for a task class, the runner does
not advertise or claim that class.

The module is split into two layers so the security-critical logic is pure and
unit-testable without any sandbox actually installed:

  * **Capability detection** (:func:`detect_capabilities`) probes the host once
    at startup: usable container runtime (``docker info`` must actually succeed —
    installed-but-permission-denied reads as UNUSABLE), ``apptainer``/``enroot``/
    ``podman``, unprivileged user namespaces, ``bwrap``/``unshare``, GPU indices,
    and ``systemd-run``/cgroup availability.
  * **Pure builders** (decision table, GPU-index assignment, env allowlist, and
    the bwrap / docker / apptainer argv builders) take a :class:`Capabilities`
    value plus task fields and return a plan. These have no side effects and are
    exhaustively covered by ``scripts/test_runner_isolation.py``.

Stdlib-only (Python 3.10+). ``scripts/local_runner.py`` imports this module.

Runtime note: ``bwrap``/``apptainer`` are NOT installed on the development box,
so the assembled sandbox commands cannot be *executed* in CI here — only their
argv is asserted. On-box runtime validation (a real ``bwrap`` launch on the DGX)
is pending an operator install of bubblewrap.
"""

from __future__ import annotations

import os
import posixpath
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field

# ── Task classes / backends ──────────────────────────────────────────────────

CLASS_AGENT = "agent"
CLASS_COMPUTE = "compute"

BACKEND_BWRAP = "bwrap"
BACKEND_DOCKER = "docker"
BACKEND_PODMAN = "podman"
BACKEND_APPTAINER = "apptainer"
BACKEND_ENROOT = "enroot"
DECLINE = "declined"

# Fixed in-sandbox mount points. Binding the per-task work dir at a stable path
# (instead of echoing the host path) decouples the sandbox from host layout and
# avoids the tmpfs-/tmp shadowing a host-path bind under /tmp would hit.
SANDBOX_WORKSPACE = "/workspace"
SANDBOX_HOME = "/home"


# ── Env allowlists (never os.environ.copy) ───────────────────────────────────

# Provider creds + LLM config an AGENT task legitimately needs. Mirrors the
# agent's own MANAGED_LLM_ENV_KEYS (agent/src/index.ts) plus TLS-trust, locale,
# Bedrock, and agent tuning knobs. Everything NOT on this list is dropped —
# critically BLUEPRINT_RUNNER_TOKEN and BLUEPRINT_ORCHESTRATOR_URL, which must
# never reach untrusted task code.
AGENT_ENV_ALLOWLIST: tuple[str, ...] = (
    # LLM routing / execution mode
    "PROVIDER",
    "AUTH_MODE",
    "LLM_MODEL",
    "LLM_EXECUTION_MODE",
    "LLM_SETTINGS",
    "LLM_CLI_TRANSLATION",
    "LLM_NO_PROGRESS_STALL_MS",
    "WORKFLOW_TOOL_STALL_TIMEOUT_MS",
    # Anthropic / Claude
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    # Z.ai
    "ZAI_API_KEY",
    "ZAI_ANTHROPIC_BASE_URL",
    "ZAI_BASE_URL",
    # OpenAI / Codex
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CODEX_HOME",
    # Gemini / Vertex
    "GEMINI_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GEMINI_CLI_TRUST_WORKSPACE",
    # Bedrock (AWS)
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    # TLS trust roots (so egress to the LLM API / GitHub validates)
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    # Locale / terminal sanity for spawned tools
    "LANG",
    "LC_ALL",
    "TERM",
    # Agent knobs
    "GIT_REFERENCE_CACHE_ROOT",
)

# Host env a COMPUTE job may inherit. Deliberately EXCLUDES all provider creds —
# untrusted experiment code has no business seeing LLM keys. Only toolchain/CUDA
# discovery vars pass through; the job's own declared env and the injected
# BLUEPRINT_* result vars are layered on top by build_compute_env.
COMPUTE_ENV_PASSTHROUGH: tuple[str, ...] = (
    "LANG",
    "LC_ALL",
    "TERM",
    "LD_LIBRARY_PATH",
    "CUDA_HOME",
    "CUDA_PATH",
)

# Env keys the runner must NEVER hand to any task (defense-in-depth assertion
# target; they are simply absent from the allowlists above).
FORBIDDEN_ENV_KEYS: frozenset[str] = frozenset(
    {"BLUEPRINT_RUNNER_TOKEN", "BLUEPRINT_ORCHESTRATOR_URL"}
)

# Standard PATH inside a sandbox (host $HOME-relative dirs like nvm are gone, so
# we build PATH from system dirs plus each bound tool's directory).
_DEFAULT_SANDBOX_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Base read-only binds every sandbox needs for a working toolchain + egress.
# --ro-bind-try is used for all so a path absent on a given distro is skipped.
_BASE_RO_BINDS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/lib32",
    "/etc/alternatives",
    "/etc/ssl",
    "/etc/pki",
    "/etc/ca-certificates",
    "/etc/ca-certificates.conf",
    "/etc/resolv.conf",
    "/etc/hosts",
    "/etc/nsswitch.conf",
    "/etc/gai.conf",
)


# ── Capabilities ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Capabilities:
    """Immutable snapshot of the host's isolation + accelerator capabilities."""

    bwrap_path: str | None = None
    unshare_path: str | None = None
    userns_ok: bool = False
    docker_usable: bool = False
    # Daemon accepts CDI GPU devices (--device nvidia.com/gpu=N). Rootless/CDI
    # daemons reject the legacy --gpus flag, so the argv builder keys on this.
    docker_cdi_gpu: bool = False
    podman_usable: bool = False
    apptainer_path: str | None = None
    enroot_path: str | None = None
    systemd_run_path: str | None = None
    gpu_indices: tuple[int, ...] = ()
    gpu_names: tuple[str, ...] = ()
    # Human-readable notes surfaced in the preflight (e.g. "docker socket denied").
    notes: tuple[str, ...] = ()

    # ── Derived predicates ──
    @property
    def bwrap_ok(self) -> bool:
        """A rootless bwrap sandbox can be built: binary present AND userns on."""
        return bool(self.bwrap_path) and self.userns_ok

    @property
    def has_gpu(self) -> bool:
        return bool(self.gpu_indices)

    def any_container_runtime(self) -> bool:
        return (
            self.docker_usable
            or self.podman_usable
            or bool(self.apptainer_path)
            or bool(self.enroot_path)
        )

    def preferred_container(self) -> str | None:
        """Pick a container backend; docker/podman first (image-native), then HPC."""
        if self.docker_usable:
            return BACKEND_DOCKER
        if self.podman_usable:
            return BACKEND_PODMAN
        if self.apptainer_path:
            return BACKEND_APPTAINER
        if self.enroot_path:
            return BACKEND_ENROOT
        return None


def _which(name: str) -> str | None:
    return shutil.which(name)


def probe_userns(unshare_path: str | None = None) -> bool:
    """Return True if unprivileged user namespaces can be created.

    Order of evidence:
      1. ``/proc/sys/kernel/unprivileged_userns_clone`` — honor it if present
         ("1" → yes, "0" → no). Debian/Ubuntu expose this knob.
      2. If the sysctl is absent (many kernels removed it and default-enable
         userns), fall back to a FUNCTIONAL probe: ``unshare --user --map-root-user
         true``. This actually attempts the syscall rather than guessing.
      3. If neither is decisive → False (fail-closed: no sandbox → no claim).
    """
    sysctl = "/proc/sys/kernel/unprivileged_userns_clone"
    try:
        with open(sysctl, encoding="ascii") as fh:
            raw = fh.read().strip()
        if raw == "1":
            return True
        if raw == "0":
            return False
    except OSError:
        pass  # sysctl not present — fall through to functional probe

    probe_bin = unshare_path or _which("unshare")
    if not probe_bin:
        return False
    try:
        result = subprocess.run(
            [probe_bin, "--user", "--map-root-user", "true"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def probe_container_runtime_usable(binary: str, *, subcommand: str = "info") -> bool:
    """Return True only if ``<binary> <subcommand>`` actually SUCCEEDS.

    This is the crux of the "installed-but-denied reads as unusable" requirement:
    on the shared DGX ``docker`` is on PATH but ``docker info`` returns non-zero
    (permission denied on the socket). Presence on PATH is NOT sufficient — we
    require a zero exit, so a denied socket correctly reads as unusable and the
    runner falls back to bwrap/apptainer instead of trying (and failing) docker.
    """
    if not _which(binary):
        return False
    try:
        result = subprocess.run(
            [binary, subcommand],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def probe_docker_cdi_gpu(binary: str = "docker") -> bool:
    """True iff the docker daemon exposes CDI spec dirs AND an ``nvidia.com/gpu``
    CDI spec is actually present in one of them.

    Rootless/CDI-configured daemons (``features.cdi`` + a spec in /etc/cdi or a
    user spec dir) reject the legacy ``--gpus`` flag ("could not select device
    driver"), while nvidia-runtime-hook daemons don't know CDI device names —
    so :func:`build_docker_argv` must pick the GPU flag per daemon. Detection is
    generic: ask the daemon for its CDISpecDirs (empty/absent on pre-CDI or
    cdi-disabled daemons) and scan the readable ones for a spec mentioning the
    ``nvidia.com/gpu`` device kind. Any failure reads as False → the builder
    keeps the legacy flag, i.e. unchanged behavior on hook-based hosts.
    """
    try:
        result = subprocess.run(
            [binary, "info", "--format", "{{range .CDISpecDirs}}{{println .}}{{end}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    for spec_dir in (line.strip() for line in (result.stdout or "").splitlines()):
        if not spec_dir:
            continue
        try:
            names = os.listdir(spec_dir)
        except OSError:
            continue
        for name in names:
            if not name.endswith((".yaml", ".yml", ".json")):
                continue
            try:
                with open(
                    os.path.join(spec_dir, name), encoding="utf-8", errors="replace"
                ) as fh:
                    if "nvidia.com/gpu" in fh.read(65536):
                        return True
            except OSError:
                continue
    return False


def probe_systemd_run() -> str | None:
    """Return the systemd-run path if a user scope can plausibly be created."""
    path = _which("systemd-run")
    if not path:
        return None
    # A user manager must be reachable for --user scopes. Best-effort: presence of
    # XDG_RUNTIME_DIR with a systemd bus is the common signal; we still return the
    # path and let the resource-cap wrapper degrade gracefully if the scope fails.
    return path


def detect_gpus() -> tuple[tuple[int, ...], tuple[str, ...]]:
    """Return (indices, names) from ``nvidia-smi -L``; empty on any failure."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return (), ()
    if result.returncode != 0:
        return (), ()
    indices: list[int] = []
    names: list[str] = []
    for m in re.finditer(
        r"^GPU (\d+):\s*(.+?)(?:\s*\(UUID:.+)?\s*$", result.stdout, re.MULTILINE
    ):
        indices.append(int(m.group(1)))
        names.append(m.group(2).strip())
    return tuple(indices), tuple(names)


def detect_capabilities() -> Capabilities:
    """Probe the host once. Every probe is non-fatal (absent tool → unusable)."""
    bwrap_path = _which("bwrap")
    unshare_path = _which("unshare")
    userns_ok = probe_userns(unshare_path)

    docker_present = _which("docker") is not None
    docker_usable = probe_container_runtime_usable("docker") if docker_present else False
    docker_cdi_gpu = probe_docker_cdi_gpu() if docker_usable else False
    podman_usable = probe_container_runtime_usable("podman")
    apptainer_path = _which("apptainer") or _which("singularity")
    enroot_path = _which("enroot")
    systemd_run_path = probe_systemd_run()
    gpu_indices, gpu_names = detect_gpus()

    notes: list[str] = []
    if docker_present and not docker_usable:
        notes.append("docker present but 'docker info' failed (socket denied?) — unusable")
    if bwrap_path and not userns_ok:
        notes.append("bwrap present but unprivileged userns unavailable — bwrap unusable")

    return Capabilities(
        bwrap_path=bwrap_path,
        unshare_path=unshare_path,
        userns_ok=userns_ok,
        docker_usable=docker_usable,
        docker_cdi_gpu=docker_cdi_gpu,
        podman_usable=podman_usable,
        apptainer_path=apptainer_path,
        enroot_path=enroot_path,
        systemd_run_path=systemd_run_path,
        gpu_indices=gpu_indices,
        gpu_names=gpu_names,
        notes=tuple(notes),
    )


# ── Decision table (pure, fail-closed) ───────────────────────────────────────

# Read-only agent task types eligible for the prod agent CONTAINER image. This is
# the "read-only slice": types whose agent runs in read_only mode (they never push
# a branch/PR) AND whose work is light — no postgres, no pytest, no
# docker-in-docker — so the slim prod agent image (agent/Dockerfile) satisfies
# them at exact prod parity (Cloud Run Jobs / GCE VMs run the same image).
#
# Reuse note: the backend's authoritative read-only registry is
# ``app.task_capabilities.READ_ONLY_TASK_TYPES`` (re-exported for Cloud Run Jobs
# as ``CLOUD_RUN_CANDIDATE_TASK_TYPES``). It is deliberately NOT imported here —
# runner_isolation is stdlib-only (see the module docstring) and must not pull in
# the backend package — and it is NOT identical to what belongs here: that set
# also contains the heavy ``requires_compute`` read-only types (smoketest / perf /
# verify_tests / compute_experiment), which need pytest / a DB / the compute
# substrate and therefore MUST NOT be routed to this thin read-only image (cf.
# agent/Dockerfile header). This frozenset is the hand-maintained read-only slice:
# the registry's read_only types MINUS those compute-heavy ones (smoketest / perf /
# verify_tests / compute_experiment). It contains ONLY read_only (agent_mode !=
# "write") types — never a WRITE/PR type such as ``docs`` (output_kind="pr"), which
# would push a branch the read-only image cannot safely produce. Keep it in sync
# with the registry when a new light read-only type is added.
AGENT_DOCKER_TYPES: frozenset[str] = frozenset(
    {
        "strategic_review",
        "design_doc_update",
        "research",
        "web_research",
        "plan",
        "design",
        "memo",
        "investigate",
        "debug",
        "self_audit",
        "security_audit",
        "review",
        "panel_review",
        "release_audit",
        "release_audit_challenge",
        "release_merge_grade",
        "repo_bootstrap",
    }
)


def decide_agent_backend(
    caps: Capabilities,
    agent_binary_present: bool,
    *,
    task_type: str | None = None,
    agent_image: str | None = None,
) -> str:
    """Choose an agent task's isolation backend, fail-closed.

    **Policy: bwrap-first.** A container backend is used only when a documented
    necessity holds — never merely because a container runtime is present.  The
    three documented necessities for the agent slice are:

      1. **Prod-parity opt-in**: ``agent_image`` is explicitly supplied by the
         launcher AND docker is usable AND the task type is read-only-eligible
         (``task_type is None`` or in :data:`AGENT_DOCKER_TYPES`).  Running the
         prod agent container gives exact Cloud-Run / GCE parity and is an
         explicit operator choice, not a default.
      2. **bwrap unbuildable**: ``caps.bwrap_ok`` is false (binary absent or
         unprivileged userns disabled).  No boundary other than a container is
         available; the runner declines if no container route exists either.
      3. *(Compute only — not agent)*: image-native jobs that supply a
         ``container_image`` the experiment must run inside.

    Backends, in preference order:

      * ``BACKEND_DOCKER`` — ``agent_image`` supplied AND docker usable AND
        task type read-only-eligible (documented necessity #1 above).  This
        path does NOT require ``agent_binary_present``: the image ships its
        own agent build, so a host ``dist/index.js`` is irrelevant to it.
        Historical note: this path was introduced to work around an EBUSY
        crash when bwrap-sandboxed agents ran ``rm -rf /workspace`` on a
        bind-mount point; that root cause is now fixed in workspacePrep.ts
        (empty-in-place instead of rm-dir), so the docker path is a pure
        opt-in for prod-parity runs.
      * ``BACKEND_BWRAP`` — preferred default: no eligible docker image (or
        docker is unusable), host agent binary present, rootless bwrap sandbox
        available.  This covers WSL PC, second PC, DGX (userns on), and lab
        server for the common imageless-agent case.
      * ``DECLINE`` — neither boundary can be built.

    CRITICAL invariant: when ``agent_image`` is falsy the result is byte-identical
    to the historical bwrap-or-decline table — the docker branch is inert.
    """
    # Preferred: rootless bwrap when binary + sandbox are both available.
    if agent_binary_present and caps.bwrap_ok:
        return BACKEND_BWRAP
    # Reserved docker fallback: bwrap unavailable, but prod image + usable docker.
    if (
        agent_image
        and caps.docker_usable
        and (task_type is None or task_type in AGENT_DOCKER_TYPES)
    ):
        return BACKEND_DOCKER
    if not agent_binary_present:
        return DECLINE
    return DECLINE


def agent_decline_reason(caps: Capabilities, agent_binary_present: bool) -> str | None:
    if not agent_binary_present:
        return "agent binary not built (run: cd agent && npm run build)"
    if not caps.bwrap_ok:
        return "no bwrap sandbox (install bubblewrap; kernel userns required)"
    return None


def decide_compute_backend(caps: Capabilities, has_image: bool) -> str:
    """Choose a compute backend, fail-closed.

    Table (image? × runtime? × bwrap?):
      image + container-runtime          → container backend (docker/podman/apptainer/enroot)
      image + no runtime + bwrap         → bwrap (best-effort on host toolchain; image unfulfillable)
      no image + bwrap                   → bwrap
      no image + no bwrap                → DECLINE (a container needs an image; nothing else is sandboxed)
      image + no runtime + no bwrap      → DECLINE
    """
    if has_image and caps.any_container_runtime():
        return caps.preferred_container() or DECLINE
    if caps.bwrap_ok:
        return BACKEND_BWRAP
    return DECLINE


def compute_decline_reason(caps: Capabilities, has_image: bool) -> str | None:
    backend = decide_compute_backend(caps, has_image)
    if backend != DECLINE:
        return None
    if has_image:
        return (
            "container_image requested but no usable container runtime AND no bwrap "
            "sandbox (install a container runtime or bubblewrap)"
        )
    return "no bwrap sandbox for imageless compute (install bubblewrap)"


def capabilities_inventory(caps: Capabilities) -> dict[str, bool]:
    """Return the runner's isolation-backend availability inventory (JSON-safe).

    A flat ``{backend: bool}`` map surfaced in the heartbeat so the operator's
    fleet view can show WHICH isolation backends each heterogeneous device can
    actually build a sandbox with — instead of a single opaque ``agent_available``
    flag. The values are the SAME derived predicates the decision table keys on
    (``caps.bwrap_ok`` property, the ``*_usable`` probes, and ``bool(path)`` for
    the path-only HPC backends), so this can never drift from :func:`detect_capabilities`.
    Every value is a plain ``bool`` → the dict is ``json.dumps``-serializable.
    """
    return {
        "bwrap_ok": caps.bwrap_ok,
        "userns_ok": caps.userns_ok,
        "docker_usable": caps.docker_usable,
        "docker_cdi_gpu": caps.docker_cdi_gpu,
        "podman_usable": caps.podman_usable,
        "apptainer_ok": bool(caps.apptainer_path),
        "enroot_ok": bool(caps.enroot_path),
        "systemd_run_ok": bool(caps.systemd_run_path),
    }


def claimable_classes(caps: Capabilities, agent_binary_present: bool) -> dict[str, bool]:
    """What the runner may advertise/pull.

    * agent   — claimable iff an agent bwrap sandbox can be built.
    * compute — claimable iff ANY compute job could be sandboxed on this box
      (bwrap present, or a container runtime present for image jobs). Per-job
      DECLINE (e.g. imageless on a container-only box) is still enforced at
      execution time; this only governs advertisement.
    """
    return {
        CLASS_AGENT: decide_agent_backend(caps, agent_binary_present) != DECLINE,
        CLASS_COMPUTE: caps.bwrap_ok or caps.any_container_runtime(),
    }


# ── GPU index assignment ─────────────────────────────────────────────────────


def parse_gpu_allowlist(env_value: str | None, detected: tuple[int, ...]) -> list[int]:
    """Resolve the operator's ``BLUEPRINT_RUNNER_GPU_INDICES`` against detected GPUs.

    Empty/unset → all detected. A set value is intersected with the detected
    indices (a typo that intersects to empty yields [] — GPU jobs then find no
    free device and wait/decline rather than silently using an unintended GPU).
    """
    if not env_value or not env_value.strip():
        return list(detected)
    detected_set = set(detected)
    out: list[int] = []
    for tok in env_value.split(","):
        tok = tok.strip()
        if tok.isdigit():
            idx = int(tok)
            if idx in detected_set and idx not in out:
                out.append(idx)
    return out


def assign_gpu_indices(
    allowed: list[int], in_use: set[int], per_job: int
) -> list[int] | None:
    """Assign a DISJOINT subset of ``per_job`` free GPUs, or None if unavailable.

    Concurrent compute jobs must not share a device (a 16 GB V100 fits one job).
    Returns the lowest-numbered free indices; None when fewer than ``per_job``
    are free so the caller can wait rather than double-book a GPU.
    """
    if per_job <= 0:
        return []
    free = [i for i in allowed if i not in in_use]
    if len(free) < per_job:
        return None
    return free[:per_job]


# ── Env allowlist builders ───────────────────────────────────────────────────


def _sandbox_path(tool_dirs: list[str]) -> str:
    extra = [d for d in tool_dirs if d]
    seen: set[str] = set()
    ordered: list[str] = []
    for d in [*extra, *_DEFAULT_SANDBOX_PATH.split(":")]:
        if d and d not in seen:
            seen.add(d)
            ordered.append(d)
    return ":".join(ordered)


def build_agent_env(
    source_env: dict[str, str],
    injected: dict[str, str],
    tool_dirs: list[str] | None = None,
    per_task_creds: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build an agent task's env from the ALLOWLIST only — never a copy.

    ``injected`` carries the runner-set identity vars (AGENT_SECRET_TOKEN,
    ORCHESTRATOR_URL, TASK_ID, VM_INSTANCE_ID, AGENT_WORKSPACE). PATH is rebuilt
    from the bound tool dirs + system dirs; HOME defaults to the tmpfs sandbox
    home. BLUEPRINT_RUNNER_TOKEN / BLUEPRINT_ORCHESTRATOR_URL are structurally
    excluded (absent from AGENT_ENV_ALLOWLIST).

    When ``per_task_creds`` is provided (server-vended per-task credentials), the
    LLM credential keys from per_task_creds REPLACE any matching keys from
    source_env.  This is the SINGLE credential code path: no fallback to static
    host env vars for credential keys.  Non-credential keys (LANG, TERM, PATH,
    NODE_EXTRA_CA_CERTS, etc.) still flow from source_env.  per_task_creds values
    are still filtered through AGENT_ENV_ALLOWLIST for defense-in-depth.
    """
    env: dict[str, str] = {}
    for key in AGENT_ENV_ALLOWLIST:
        val = source_env.get(key)
        if val:
            env[key] = val
    # Apply the runner-set identity vars (injected) first, then overlay the
    # server-vended per-task credentials so per_task_creds REPLACES any matching
    # keys from both source_env and injected (e.g. injected's llm_env falls back
    # to a static host value but per_task_creds carries the true per-task value).
    # Only keys in AGENT_ENV_ALLOWLIST pass through; everything else is dropped
    # (defense-in-depth).
    env.update({k: v for k, v in injected.items() if v is not None})
    if per_task_creds:
        for key, val in per_task_creds.items():
            if key in AGENT_ENV_ALLOWLIST and val:
                env[key] = val
    env["PATH"] = _sandbox_path(tool_dirs or [])
    env.setdefault("HOME", SANDBOX_HOME)
    # Defense in depth: strip anything forbidden that slipped through injected.
    for forbidden in FORBIDDEN_ENV_KEYS:
        env.pop(forbidden, None)
    return env


def build_compute_env(
    job_env: dict[str, str],
    injected: dict[str, str],
    source_env: dict[str, str],
) -> dict[str, str]:
    """Build a compute job's env: passthrough allowlist + declared env + injected.

    No provider creds (untrusted experiment code never sees LLM keys) and never a
    full os.environ copy. ``injected`` = BLUEPRINT_RESULT_FILE, BLUEPRINT_ARTIFACT_DIR,
    CUDA_VISIBLE_DEVICES/NVIDIA_VISIBLE_DEVICES, PYTHONPATH, HOME.
    """
    env: dict[str, str] = {}
    for key in COMPUTE_ENV_PASSTHROUGH:
        val = source_env.get(key)
        if val:
            env[key] = val
    env.update({k: str(v) for k, v in job_env.items()})
    env.update({k: v for k, v in injected.items() if v is not None})
    env.setdefault("PATH", _DEFAULT_SANDBOX_PATH)
    env.setdefault("HOME", SANDBOX_HOME)
    for forbidden in FORBIDDEN_ENV_KEYS:
        env.pop(forbidden, None)
    return env


# ── GPU device / driver exposure ─────────────────────────────────────────────


def gpu_device_nodes(indices: list[int]) -> list[str]:
    """Return the /dev device nodes to bind for the assigned GPU indices.

    Control nodes (nvidiactl, nvidia-uvm[-tools], nvidia-modeset) are shared;
    per-index nodes (/dev/nvidiaN) are exposed ONLY for the assigned devices so
    a job cannot touch a GPU it was not assigned.
    """
    nodes = [
        "/dev/nvidiactl",
        "/dev/nvidia-uvm",
        "/dev/nvidia-uvm-tools",
        "/dev/nvidia-modeset",
    ]
    nodes += [f"/dev/nvidia{idx}" for idx in indices]
    return nodes


def nvidia_driver_libs(ldconfig_output: str) -> list[str]:
    """Extract host NVIDIA driver userspace lib paths from ``ldconfig -p`` output.

    Used to bind driver libs that live OUTSIDE the wholesale-bound /usr,/lib
    roots (rare, but some driver installs stage libs elsewhere). Lines look like:
        libcuda.so.1 (libc6,x86-64) => /usr/lib/x86_64-linux-gnu/libcuda.so.1
    """
    wanted = ("libcuda", "libnvidia", "libcudadebugger", "libnvcuvid", "libnvoptix")
    libs: list[str] = []
    for line in ldconfig_output.splitlines():
        if "=>" not in line:
            continue
        path = line.split("=>", 1)[1].strip()
        base = os.path.basename(path)
        if any(base.startswith(w) for w in wanted) and path not in libs:
            libs.append(path)
    return libs


def _covered_by(path: str, roots: tuple[str, ...]) -> bool:
    # Always POSIX semantics — these are Linux sandbox paths even when the unit
    # tests run on Windows (os.path.normpath would use backslashes there).
    norm = posixpath.normpath(path)
    for root in roots:
        r = posixpath.normpath(root)
        if norm == r or norm.startswith(r + "/"):
            return True
    return False


# ── bwrap argv builder ───────────────────────────────────────────────────────


def build_bwrap_argv(
    *,
    bwrap_path: str,
    command_argv: list[str],
    workspace_host: str,
    chdir: str = SANDBOX_WORKSPACE,
    ro_binds: list[str] | None = None,
    tool_paths: list[str] | None = None,
    gpu_device_nodes_list: list[str] | None = None,
    extra_ro_libs: list[str] | None = None,
) -> list[str]:
    """Assemble a rootless bwrap invocation.

    Guarantees (asserted in tests):
      * Namespaces: user/ipc/pid/uts unshared, cgroup best-effort; --die-with-parent
        and --new-session so the whole tree dies with the runner and detaches TTY.
      * Fresh tmpfs /home and /tmp — NO host $HOME is ever bound.
      * Read-only minimal toolchain (/usr,/bin,/lib,... via --ro-bind-try) + the
        resolved tool paths; the per-task workspace bound RW at /workspace.
      * Network is NOT unshared (agent needs LLM + GitHub egress).
      * NO docker socket, NO host home. GPU nodes added only when provided.
    """
    ro = ro_binds if ro_binds is not None else list(_BASE_RO_BINDS)
    tools = tool_paths or []
    gpu_nodes = gpu_device_nodes_list or []
    libs = extra_ro_libs or []

    argv: list[str] = [
        bwrap_path,
        "--unshare-user",
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--die-with-parent",
        "--new-session",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", SANDBOX_HOME,
    ]

    # Read-only toolchain / config. --ro-bind-try skips paths absent on this host.
    seen_src: set[str] = set()
    for src in [*ro, *tools, *libs]:
        if src and src not in seen_src:
            seen_src.add(src)
            argv += ["--ro-bind-try", src, src]

    # GPU device nodes AFTER --dev so they layer into the new /dev.
    for node in gpu_nodes:
        argv += ["--dev-bind-try", node, node]

    # Per-task workspace, read-write, at a stable path.
    argv += ["--bind", workspace_host, SANDBOX_WORKSPACE]
    if chdir:
        argv += ["--chdir", chdir]

    argv += ["--"]
    argv += command_argv
    return argv


def tool_dirs_for(paths: list[str]) -> list[str]:
    """Directories of resolved tool paths, for constructing the sandbox PATH."""
    dirs: list[str] = []
    for p in paths:
        if not p:
            continue
        d = os.path.dirname(p)
        if d and d not in dirs:
            dirs.append(d)
    return dirs


def resolve_tools(names: list[str], source_env: dict[str, str] | None = None) -> list[str]:
    """Resolve tool names to realpaths (skipping any not found)."""
    path_env = (source_env or {}).get("PATH") if source_env else None
    out: list[str] = []
    for name in names:
        found = shutil.which(name, path=path_env)
        if found:
            real = os.path.realpath(found)
            if real not in out:
                out.append(real)
            if found not in out:
                out.append(found)
    return out


# ── Container argv builders ──────────────────────────────────────────────────


def build_docker_argv(
    *,
    image: str,
    command: str | None,
    container_name: str,
    gpu_indices: list[int] | None,
    binds: list[tuple[str, str]],
    env_keys: list[str],
    cwd: str | None,
    cpu_limit: str | None = None,
    mem_limit: str | None = None,
    runtime: str = BACKEND_DOCKER,
    gpu_cdi: bool = False,
) -> list[str]:
    """docker/podman run argv. Env VALUES are passed via ``-e KEY`` (read from the
    subprocess env), never inlined as ``KEY=VALUE`` on argv (which /proc/cmdline
    would expose). GPUs are scoped to the assigned indices only: legacy
    ``--gpus device=<idx,...>`` for nvidia-runtime-hook daemons, or one
    ``--device nvidia.com/gpu=<idx>`` per GPU when ``gpu_cdi`` is set (CDI
    daemons reject ``--gpus``, hook daemons don't know CDI names — the caller
    picks via :func:`probe_docker_cdi_gpu`).

    ``command`` semantics:
      * a string → the image is run with a ``bash -lc <command>`` override
        (the compute-job path: the image is a generic toolchain, the command is
        the experiment).
      * ``None`` → NO command override is appended; the image runs its own
        ENTRYPOINT/CMD (the agent path: agent/Dockerfile's ``CMD ["npm","start"]``
        is the whole contract — the agent claims one task and exits).
    """
    argv: list[str] = [runtime, "run", "--rm", "--name", container_name]
    if gpu_indices:
        if gpu_cdi:
            for i in gpu_indices:
                argv += ["--device", f"nvidia.com/gpu={i}"]
        else:
            argv += ["--gpus", "device=" + ",".join(str(i) for i in gpu_indices)]
    if cpu_limit:
        argv += ["--cpus", str(cpu_limit)]
    if mem_limit:
        argv += ["--memory", str(mem_limit)]
    for host, cont in binds:
        argv += ["-v", f"{host}:{cont}"]
    for key in env_keys:
        argv += ["-e", key]  # value pulled from the runner's env for this exec
    if cwd:
        argv += ["-w", cwd]
    argv += [image]
    if command is not None:
        argv += ["bash", "-lc", command]
    return argv


def normalize_apptainer_image(image: str) -> str:
    """Apptainer needs a URI; an OCI/registry ref becomes ``docker://<ref>``."""
    if "://" in image:
        return image
    return f"docker://{image}"


def build_apptainer_argv(
    *,
    apptainer_path: str,
    image: str,
    command: str,
    gpu: bool,
    binds: list[tuple[str, str]],
    cwd: str | None,
) -> list[str]:
    """apptainer exec argv. ``--containall --no-home`` is MANDATORY — apptainer
    otherwise mounts the host $HOME and $PWD by default, defeating isolation.
    ``--nv`` exposes the driver stack; GPU scoping is via APPTAINERENV_* env (set
    by :func:`apptainer_env`), keeping device selection off argv.
    """
    argv: list[str] = [apptainer_path, "exec", "--containall", "--no-home"]
    if gpu:
        argv += ["--nv"]
    for host, cont in binds:
        argv += ["--bind", f"{host}:{cont}"]
    if cwd:
        argv += ["--pwd", cwd]
    argv += [normalize_apptainer_image(image), "bash", "-lc", command]
    return argv


def apptainer_env(
    inner_env: dict[str, str],
    registry_auth: dict[str, str] | None = None,
) -> dict[str, str]:
    """Translate an in-container env dict to APPTAINERENV_* vars (kept off argv).

    Also maps registry credentials to APPTAINER_DOCKER_USERNAME/PASSWORD so a
    private (e.g. NGC) pull authenticates without the secret touching argv.
    """
    out: dict[str, str] = {}
    for key, val in inner_env.items():
        if val is not None:
            out[f"APPTAINERENV_{key}"] = str(val)
    if registry_auth:
        user = registry_auth.get("username")
        password = registry_auth.get("password")
        if user:
            out["APPTAINER_DOCKER_USERNAME"] = str(user)
        if password:
            out["APPTAINER_DOCKER_PASSWORD"] = str(password)
    return out


# ── Resource caps (best-effort, NOT fail-closed) ─────────────────────────────


def build_resource_scope_prefix(
    caps: Capabilities,
    *,
    cpu_limit: str | None,
    mem_limit: str | None,
    scope_name: str | None = None,
) -> list[str]:
    """Return a ``systemd-run --user --scope`` prefix, or [] if unavailable.

    Wraps the sandbox in a transient user scope with CPUQuota / MemoryMax when
    systemd-run is present. When it is not, the caller falls back to rlimits and,
    failing that, WARNs and proceeds (resource caps are a good-neighbor best
    effort, never a fail-closed gate).
    """
    if not (cpu_limit or mem_limit):
        return []
    if not caps.systemd_run_path:
        return []
    argv = [caps.systemd_run_path, "--user", "--scope", "--collect", "--quiet"]
    if scope_name:
        argv += ["--unit", scope_name]
    if cpu_limit:
        try:
            quota_pct = max(1, int(round(float(cpu_limit) * 100)))
            argv += ["-p", f"CPUQuota={quota_pct}%"]
        except (TypeError, ValueError):
            pass
    if mem_limit:
        argv += ["-p", f"MemoryMax={mem_limit}"]
    return argv


def rlimit_preexec(mem_limit: str | None):
    """Return a preexec_fn setting RLIMIT_AS from ``mem_limit`` (bytes), or None.

    POSIX-only best-effort fallback when systemd-run is unavailable. Accepts a
    plain byte count or a suffix form (e.g. "4g", "512m").
    """
    nbytes = _parse_mem_bytes(mem_limit)
    if nbytes is None:
        return None
    try:
        import resource
    except ImportError:
        return None

    def _apply() -> None:  # pragma: no cover - runs in the child, POSIX only
        resource.setrlimit(resource.RLIMIT_AS, (nbytes, nbytes))

    return _apply


def _parse_mem_bytes(value: str | None) -> int | None:
    if not value:
        return None
    text = str(value).strip().lower()
    mult = 1
    if text.endswith(("k", "kb")):
        mult, text = 1024, text.rstrip("kb")
    elif text.endswith(("m", "mb")):
        mult, text = 1024**2, text.rstrip("mb")
    elif text.endswith(("g", "gb")):
        mult, text = 1024**3, text.rstrip("gb")
    elif text.endswith("b"):
        text = text.rstrip("b")
    try:
        return int(float(text) * mult)
    except ValueError:
        return None


# ── Process-group teardown ───────────────────────────────────────────────────

# SIGKILL is POSIX-only. On Windows (dev box for the unit tests) it is absent;
# fall back to SIGTERM there — proc.kill()/terminate() both map to TerminateProcess.
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def terminate_process_tree(
    proc,
    *,
    backend: str | None = None,
    container_name: str | None = None,
    grace: float = 5.0,
    runtime_path: str | None = None,
) -> None:
    """Tear down a task's whole process tree, escalating SIGTERM → SIGKILL.

    For docker/podman the workload runs under the daemon, so killing our client's
    process group is not enough — issue ``<runtime> stop`` by the container name.
    For bwrap/apptainer (which run in our own group, spawned start_new_session)
    we signal the process GROUP so no orphaned child survives.
    """
    if backend in (BACKEND_DOCKER, BACKEND_PODMAN) and container_name:
        rt = runtime_path or backend
        try:
            subprocess.run(
                [rt, "stop", "--time", str(int(grace)), container_name],
                capture_output=True,
                timeout=grace + 10,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    _signal_group(proc, signal.SIGTERM)
    deadline = time.time() + grace
    while time.time() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.1)
    _signal_group(proc, _SIGKILL)


def _signal_group(proc, sig) -> None:
    """Signal the child's process group, falling back to the process itself."""
    pid = getattr(proc, "pid", None)
    if pid is None:
        return
    getpgid = getattr(os, "getpgid", None)
    killpg = getattr(os, "killpg", None)
    if getpgid is not None and killpg is not None:
        try:
            killpg(getpgid(pid), sig)
            return
        except (OSError, ProcessLookupError):
            pass
    # Windows / no process-group support: signal the process directly.
    try:
        if sig == _SIGKILL and sig != signal.SIGTERM:
            proc.kill()
        else:
            proc.terminate()
    except (OSError, ProcessLookupError):
        pass


def sweep_stale_containers(caps: Capabilities, *, name_prefix: str = "bp-") -> None:
    """Best-effort startup sweep of leftover tagged containers from a prior crash."""
    for runtime, usable in (
        (BACKEND_DOCKER, caps.docker_usable),
        (BACKEND_PODMAN, caps.podman_usable),
    ):
        if not usable:
            continue
        try:
            listed = subprocess.run(
                [runtime, "ps", "-aq", "--filter", f"name={name_prefix}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
            if ids:
                subprocess.run(
                    [runtime, "rm", "-f", *ids], capture_output=True, timeout=30
                )
        except (OSError, subprocess.SubprocessError):
            continue


# ── Execution plan ───────────────────────────────────────────────────────────


@dataclass
class ExecutionPlan:
    """Everything :mod:`local_runner` needs to launch (or decline) a task."""

    backend: str
    argv: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    container_name: str | None = None
    gpu_indices: list[int] = field(default_factory=list)
    declined: bool = False
    decline_reason: str | None = None

    @classmethod
    def decline(cls, reason: str) -> ExecutionPlan:
        return cls(backend=DECLINE, declined=True, decline_reason=reason)


# ── Plan orchestrators (pure given resolved host paths) ──────────────────────

# Tools an agent task needs resolvable inside the sandbox (provider CLIs + git).
_AGENT_SANDBOX_TOOLS = (
    "node",
    "git",
    "bash",
    "sh",
    "env",
    "npm",
    "npx",
    "python3",
    "uv",
    "claude",
    "codex",
    "gemini",
    "ssh",
)
# Tools an imageless bwrap compute job needs.
_COMPUTE_SANDBOX_TOOLS = ("bash", "sh", "env", "python3", "python", "git", "uv")


def plan_agent_execution(
    *,
    caps: Capabilities,
    agent_binary: list[str] | None,
    workspace_host: str,
    injected_env: dict[str, str],
    source_env: dict[str, str],
    task_type: str | None = None,
    agent_image: str | None = None,
    per_task_creds: dict[str, str] | None = None,
) -> ExecutionPlan:
    """Build the isolation plan for an agent task, or DECLINE (fail-closed).

    ``injected_env`` = the runner-set identity vars (AGENT_SECRET_TOKEN,
    ORCHESTRATOR_URL, TASK_ID, VM_INSTANCE_ID).

    ``per_task_creds`` = server-vended per-task LLM credential env vars (optional).
    When provided, these REPLACE any matching credential keys from source_env AND
    injected_env — this is the SINGLE credential code path with no fallback to
    static host env vars.  Non-credential keys (LANG, TERM, PATH,
    NODE_EXTRA_CA_CERTS, etc.) still flow from source_env.  Applied to both bwrap
    and docker backends.

    When ``agent_image`` is supplied and docker is usable for an eligible
    ``task_type`` (see :func:`decide_agent_backend` / :data:`AGENT_DOCKER_TYPES`),
    the plan runs the prod agent CONTAINER (exact Cloud-Run/VM parity): the
    container owns ``/workspace`` as a writable image layer, keeps its own
    PATH/HOME/WORKDIR, and runs its own CMD — so nothing is bound from the host
    and neither the command nor cwd is overridden. Otherwise the historical bwrap
    plan is built unchanged (AGENT_WORKSPACE and HOME forced to in-sandbox paths).
    """
    present = agent_binary is not None
    backend = decide_agent_backend(
        caps, present, task_type=task_type, agent_image=agent_image
    )
    if backend == DECLINE:
        return ExecutionPlan.decline(
            agent_decline_reason(caps, present) or "no agent sandbox"
        )

    # ---- docker (prod agent image; exact Cloud-Run Jobs / GCE VM parity) ------
    if backend == BACKEND_DOCKER:
        assert agent_image
        task_id = injected_env.get("TASK_ID") or "task"
        container_name = f"bp-agent-{task_id[:12]}"
        # Forward ONLY the allowlisted provider creds present on the host PLUS the
        # injected identity vars — never a full environ copy, never the runner
        # token. The container owns /workspace (a writable image layer dir), its
        # own PATH/HOME/WORKDIR, and runs its own CMD (npm start → tsx src/index.ts),
        # so we bind NOTHING from the host (a /workspace bind reintroduces the bwrap
        # rm -rf EBUSY bug — this is the whole point) and override neither cwd nor
        # the command.
        forwarded: dict[str, str] = {}
        for key in AGENT_ENV_ALLOWLIST:
            val = source_env.get(key)
            if val:
                forwarded[key] = val
        # Apply the runner-set identity vars (injected_env) first, then overlay
        # server-vended per-task credentials so per_task_creds REPLACES any
        # matching keys from both source_env and injected_env.  Mirrors the
        # bwrap path in build_agent_env.
        forwarded.update({k: v for k, v in injected_env.items() if v is not None})
        if per_task_creds:
            for key, val in per_task_creds.items():
                if key in AGENT_ENV_ALLOWLIST and val:
                    forwarded[key] = val
        for forbidden in FORBIDDEN_ENV_KEYS:
            forwarded.pop(forbidden, None)
        argv = build_docker_argv(
            image=agent_image,
            command=None,            # run the image's own CMD — do NOT override
            container_name=container_name,
            gpu_indices=None,        # agent tasks are never GPU
            binds=[],                # NO host /workspace bind — the container owns it
            env_keys=sorted(forwarded),
            cwd=None,                # honor the image WORKDIR
            runtime=BACKEND_DOCKER,
        )
        # docker CLIENT env: minimal trusted vars to FIND + configure the runtime,
        # plus the forwarded values so `-e KEY` can read them for this exec. The
        # host PATH/HOME are here only so the docker binary runs on the host; they
        # are NOT in ``env_keys`` (no `-e PATH`/`-e HOME`), so the CONTAINER keeps
        # its own image PATH/HOME. Never the runner token.
        client_env: dict[str, str] = {}
        for key in (
            "PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CERT_PATH",
            "DOCKER_TLS_VERIFY", "XDG_RUNTIME_DIR",
        ):
            val = source_env.get(key)
            if val:
                client_env[key] = val
        client_env.update(forwarded)
        for forbidden in FORBIDDEN_ENV_KEYS:
            client_env.pop(forbidden, None)
        return ExecutionPlan(
            backend=backend,
            argv=argv,
            env=client_env,
            container_name=container_name,
        )

    # ---- bwrap (host toolchain; unchanged) -----------------------------------
    # agent_binary is a list[str] argv: ["node", "/path/...js"] or
    # ["/path/tsx", "/path/...ts"]. The last element is always the entry-point
    # file used to derive the package root for read-only binds.
    assert agent_binary is not None and caps.bwrap_path is not None
    entry_path = agent_binary[-1]
    agent_real = os.path.realpath(entry_path)
    # node/tsx resolves modules from the agent package root (…/agent) which
    # holds dist/, src/, and node_modules/ — bind the whole package read-only.
    agent_pkg_root = os.path.dirname(os.path.dirname(agent_real))

    tools = resolve_tools(list(_AGENT_SANDBOX_TOOLS), source_env)
    ro_binds = [*_BASE_RO_BINDS, agent_pkg_root, agent_real]

    env = build_agent_env(
        source_env,
        {
            **injected_env,
            # Point the agent at a subdirectory of the bind mount, not the
            # mountpoint itself: rm -rf on the mountpoint returns EBUSY.
            "AGENT_WORKSPACE": SANDBOX_WORKSPACE + "/agent",
            "HOME": SANDBOX_HOME,
        },
        tool_dirs_for(tools),
        per_task_creds=per_task_creds,
    )

    argv = build_bwrap_argv(
        bwrap_path=caps.bwrap_path,
        command_argv=agent_binary,
        workspace_host=workspace_host,
        ro_binds=ro_binds,
        tool_paths=tools,
    )
    return ExecutionPlan(backend=backend, argv=argv, env=env)


def _rel_to_sandbox(host_path: str | None, workspace_host: str, mount: str) -> str | None:
    """Translate a host path under ``workspace_host`` to its in-sandbox path."""
    if not host_path:
        return None
    rel = os.path.relpath(host_path, workspace_host)
    if rel.startswith(".."):
        return host_path  # not under the workspace bind; leave as-is
    return mount if rel == "." else f"{mount}/{rel.replace(os.sep, '/')}"


def plan_compute_execution(
    *,
    caps: Capabilities,
    command: str,
    image: str | None,
    registry_auth: dict[str, str] | None,
    workspace_host: str,
    result_file_host: str,
    artifact_dir_host: str,
    clone_dir_host: str | None,
    cwd_host: str | None,
    job_env: dict[str, str],
    gpu_indices: list[int],
    container_name: str,
    source_env: dict[str, str],
    ldconfig_output: str = "",
    cpu_limit: str | None = None,
    mem_limit: str | None = None,
) -> ExecutionPlan:
    """Build the sandbox plan for a compute job, or DECLINE (fail-closed).

    All ``*_host`` paths are on the runner filesystem; result_file/artifact_dir/
    clone_dir are expected to live UNDER ``workspace_host`` (the per-job tmpdir).
    """
    has_image = bool(image)
    backend = decide_compute_backend(caps, has_image)
    if backend == DECLINE:
        return ExecutionPlan.decline(
            compute_decline_reason(caps, has_image) or "no compute sandbox"
        )

    # ---- container backends (docker / podman) --------------------------------
    if backend in (BACKEND_DOCKER, BACKEND_PODMAN):
        out_mount = "/tmp/blueprint_out"
        result_in = _rel_to_sandbox(result_file_host, workspace_host, out_mount)
        artifacts_in = _rel_to_sandbox(artifact_dir_host, workspace_host, out_mount)
        binds = [(workspace_host, out_mount)]
        inner: dict[str, str] = {
            "BLUEPRINT_RESULT_FILE": result_in or f"{out_mount}/result.json",
            "BLUEPRINT_ARTIFACT_DIR": artifacts_in or f"{out_mount}/artifacts",
            "HOME": "/workspace" if clone_dir_host else out_mount,
        }
        if clone_dir_host:
            binds.append((clone_dir_host, "/workspace"))
            inner["PYTHONPATH"] = "/workspace"
        inner.update({k: str(v) for k, v in job_env.items()})
        for forbidden in FORBIDDEN_ENV_KEYS:
            inner.pop(forbidden, None)
        cwd_in = "/workspace" if clone_dir_host else cwd_host
        argv = build_docker_argv(
            image=image or "",
            command=command,
            container_name=container_name,
            gpu_indices=gpu_indices,
            binds=binds,
            env_keys=sorted(inner),
            cwd=cwd_in,
            cpu_limit=cpu_limit,
            mem_limit=mem_limit,
            runtime=backend,
            gpu_cdi=(backend == BACKEND_DOCKER and caps.docker_cdi_gpu),
        )
        # docker CLIENT env: minimal trusted vars to FIND + configure the runtime,
        # plus the inner values so `-e KEY` can forward them. Never the runner token.
        client_env: dict[str, str] = {}
        for key in (
            "PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CERT_PATH",
            "DOCKER_TLS_VERIFY", "XDG_RUNTIME_DIR",
        ):
            val = source_env.get(key)
            if val:
                client_env[key] = val
        client_env.update(inner)
        for forbidden in FORBIDDEN_ENV_KEYS:
            client_env.pop(forbidden, None)
        return ExecutionPlan(
            backend=backend, argv=argv, env=client_env,
            container_name=container_name, gpu_indices=list(gpu_indices),
        )

    # ---- apptainer / enroot (image, HPC-style) -------------------------------
    if backend in (BACKEND_APPTAINER, BACKEND_ENROOT):
        assert caps.apptainer_path is not None
        result_in = _rel_to_sandbox(result_file_host, workspace_host, "/workspace")
        artifacts_in = _rel_to_sandbox(artifact_dir_host, workspace_host, "/workspace")
        binds = [(workspace_host, "/workspace")]
        inner = {
            "BLUEPRINT_RESULT_FILE": result_in or "/workspace/result.json",
            "BLUEPRINT_ARTIFACT_DIR": artifacts_in or "/workspace/artifacts",
            "HOME": "/workspace",
        }
        clone_in = _rel_to_sandbox(clone_dir_host, workspace_host, "/workspace")
        if clone_in:
            inner["PYTHONPATH"] = clone_in
        if gpu_indices:
            inner["CUDA_VISIBLE_DEVICES"] = ",".join(str(i) for i in gpu_indices)
            inner["NVIDIA_VISIBLE_DEVICES"] = ",".join(str(i) for i in gpu_indices)
        inner.update({k: str(v) for k, v in job_env.items()})
        for forbidden in FORBIDDEN_ENV_KEYS:
            inner.pop(forbidden, None)
        cwd_in = clone_in or "/workspace"
        argv = build_apptainer_argv(
            apptainer_path=caps.apptainer_path,
            image=image or "",
            command=command,
            gpu=bool(gpu_indices),
            binds=binds,
            cwd=cwd_in,
        )
        env = apptainer_env(inner, registry_auth)
        # apptainer client needs PATH to run; never the runner token.
        if source_env.get("PATH"):
            env["PATH"] = source_env["PATH"]
        for forbidden in FORBIDDEN_ENV_KEYS:
            env.pop(forbidden, None)
        return ExecutionPlan(
            backend=backend, argv=argv, env=env,
            container_name=container_name, gpu_indices=list(gpu_indices),
        )

    # ---- bwrap (imageless, or image unfulfillable w/o a runtime) -------------
    assert caps.bwrap_path is not None
    tools = resolve_tools(list(_COMPUTE_SANDBOX_TOOLS), source_env)
    ro_binds = list(_BASE_RO_BINDS)
    gpu_nodes: list[str] = []
    extra_libs: list[str] = []
    injected: dict[str, str] = {
        "BLUEPRINT_RESULT_FILE": _rel_to_sandbox(result_file_host, workspace_host, SANDBOX_WORKSPACE)
        or f"{SANDBOX_WORKSPACE}/result.json",
        "BLUEPRINT_ARTIFACT_DIR": _rel_to_sandbox(artifact_dir_host, workspace_host, SANDBOX_WORKSPACE)
        or f"{SANDBOX_WORKSPACE}/artifacts",
        "HOME": SANDBOX_HOME,
    }
    clone_in = _rel_to_sandbox(clone_dir_host, workspace_host, SANDBOX_WORKSPACE)
    if clone_in:
        injected["PYTHONPATH"] = clone_in
    if gpu_indices:
        gpu_nodes = gpu_device_nodes(gpu_indices)
        # CUDA and NVML walk /sys/bus/pci at init to discover devices; without
        # this read-only bind cuInit fails with CUDA_ERROR_OPERATING_SYSTEM (304)
        # and nvidia-smi with "GPU access blocked by the operating system" even
        # though the /dev nodes and driver libs are all present. GPU jobs only —
        # non-GPU sandboxes stay /sys-free.
        ro_binds.append("/sys")
        # Only the assigned /dev/nvidiaN nodes are exposed, so CUDA re-enumerates
        # them from 0. Set CUDA_VISIBLE_DEVICES to that renumbered range; keep the
        # host indices in NVIDIA_VISIBLE_DEVICES for any driver hooks.
        injected["CUDA_VISIBLE_DEVICES"] = ",".join(str(i) for i in range(len(gpu_indices)))
        injected["NVIDIA_VISIBLE_DEVICES"] = ",".join(str(i) for i in gpu_indices)
        if ldconfig_output:
            extra_libs = [
                lib
                for lib in nvidia_driver_libs(ldconfig_output)
                if not _covered_by(lib, ("/usr", "/lib", "/lib64", "/lib32"))
            ]
    env = build_compute_env(job_env, injected, source_env)
    cwd_in = clone_in or (
        _rel_to_sandbox(cwd_host, workspace_host, SANDBOX_WORKSPACE) if cwd_host else SANDBOX_WORKSPACE
    )
    argv = build_bwrap_argv(
        bwrap_path=caps.bwrap_path,
        command_argv=["bash", "-lc", command],
        workspace_host=workspace_host,
        chdir=cwd_in or SANDBOX_WORKSPACE,
        ro_binds=ro_binds,
        tool_paths=tools,
        gpu_device_nodes_list=gpu_nodes,
        extra_ro_libs=extra_libs,
    )
    return ExecutionPlan(
        backend=backend, argv=argv, env=env, gpu_indices=list(gpu_indices)
    )
