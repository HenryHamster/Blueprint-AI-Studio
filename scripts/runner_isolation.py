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
import uuid
from dataclasses import dataclass, field, replace

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

PROJECT_VERIFICATION_VERSION = 1
PROJECT_VERIFICATION_PROBE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "project-verification-probe.sh"
)
PROJECT_VERIFICATION_SANDBOX_PATH = (
    f"{SANDBOX_WORKSPACE}/project-verification-probe.sh"
)
PROJECT_VERIFICATION_VENV = "/opt/blueprint-backend-venv"


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
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    # Z.ai
    "ZAI_API_KEY",
    "ZAI_ANTHROPIC_BASE_URL",
    "ZAI_BASE_URL",
    # DeepSeek's Claude-compatible adapter. These are server-vended for a
    # pooled DeepSeek task; retaining them here preserves the same local-runner
    # materialization behavior after injected values became allowlist-bound.
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_ANTHROPIC_BASE_URL",
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
    # Shared pip/uv package store (agent/src/packageStore.ts). PIP_CACHE_DIR /
    # UV_CACHE_DIR let a workspace's pip/uv point at the RW-bound shared store;
    # the BLUEPRINT_* knobs tune its location and eviction ceiling.
    "PIP_CACHE_DIR",
    "UV_CACHE_DIR",
    "BLUEPRINT_PACKAGE_STORE_ROOT",
    "BLUEPRINT_PACKAGE_STORE_MAX_BYTES",
    # Per-workspace disk reservation knobs (same names the runner's resource
    # ledger consumes): the agent re-checks its workspace's reservation at
    # creation time and fails fast when the filesystem cannot satisfy it.
    "BLUEPRINT_RUNNER_AGENT_DISK_RESERVATION_BYTES",
    "BLUEPRINT_RUNNER_MIN_DISK_BYTES",
)

# Speech credentials are never process-environment inputs. The active Resident
# fetches them through an exact call-scoped agent endpoint and retains them only
# in Coordinator memory. Keep the names solely as an explicit compute/runner
# deny-list so an obsolete host config or backend envelope cannot regress that
# boundary.
CALL_SPEECH_ENV_KEYS: frozenset[str] = frozenset(
    {
        "CALL_DEEPGRAM_API_KEY",
        "CALL_CARTESIA_API_KEY",
        "CALL_DEEPGRAM_STT_MODEL",
        "CALL_CARTESIA_TTS_MODEL",
        "CALL_CARTESIA_VOICE_ID",
        "CALL_DEEPGRAM_STT_ENDPOINT",
        "CALL_DEEPGRAM_SAMPLE_RATE_HZ",
        "CALL_DEEPGRAM_EOT_THRESHOLD",
        "CALL_DEEPGRAM_EAGER_EOT_THRESHOLD",
        "CALL_DEEPGRAM_EOT_TIMEOUT_MS",
        "CALL_CARTESIA_TTS_ENDPOINT",
        "CALL_CARTESIA_API_VERSION",
        "CALL_CARTESIA_SAMPLE_RATE_HZ",
        "CALL_SPEECH_CONNECT_TIMEOUT_MS",
    }
)

# The backend may vend only ordinary LLM credentials into an agent process.
# Call speech credentials use the Coordinator endpoint above.
SERVER_VENDED_AGENT_ENV_ALLOWLIST: frozenset[str] = AGENT_ENV_ALLOWLIST

# Identity values originate in the local runner, not in the task response.  They
# are the only non-provider values permitted in the injected map.
_AGENT_IDENTITY_ENV_KEYS: frozenset[str] = frozenset(
    {
        "AGENT_SECRET_TOKEN",
        "ORCHESTRATOR_URL",
        "TASK_ID",
        "VM_INSTANCE_ID",
        "PROJECT_ID",
        "BLUEPRINT_PRE_CLAIM_TASK_TYPE",
        "BLUEPRINT_PRE_CLAIM_RESIDENT_SESSION",
        "AGENT_WORKSPACE",
        "HOME",
        # Sandbox-side protocol dir for the walkthrough_e2e stack supervisor.
        # Non-secret; tells the agent WHERE the runner writes the supervisor
        # marker so its probe and the runner's write are one contract.
        "WALKTHROUGH_STACK_DIR",
    }
)
_AGENT_INJECTED_ENV_ALLOWLIST: frozenset[str] = frozenset(
    (*AGENT_ENV_ALLOWLIST, *_AGENT_IDENTITY_ENV_KEYS)
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
# A compute job may declare its own arbitrary non-secret configuration, but it
# must never receive Resident speech credentials even if a future caller tries
# to inject them through job_env.
COMPUTE_FORBIDDEN_ENV_KEYS: frozenset[str] = frozenset(
    (*FORBIDDEN_ENV_KEYS, *CALL_SPEECH_ENV_KEYS)
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
    "/etc/passwd",
    "/etc/group",
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
    # Functional proof produced by launching nvidia-smi through the exact
    # selected runtime/flag path used by compute jobs.  Presence of a runtime or
    # a host GPU is deliberately not proof that the two work together.
    container_gpu_passthrough: dict = field(default_factory=dict)
    podman_usable: bool = False
    apptainer_path: str | None = None
    enroot_path: str | None = None
    systemd_run_path: str | None = None
    gpu_indices: tuple[int, ...] = ()
    gpu_names: tuple[str, ...] = ()
    # Human-readable notes surfaced in the preflight (e.g. "docker socket denied").
    notes: tuple[str, ...] = ()
    # LLM CLI inventory: {"claude": {"present": bool, "version": str}, ...}
    # Probed once per heartbeat; appended to the capabilities JSONB so the
    # orchestrator can filter pool LLM runtimes to clis the runner actually has.
    llm_clis: dict = field(default_factory=dict)
    # Playwright-managed Chromium browser present on the host — the browser the
    # qualify-time walkthrough harness drives. Mirrors the repo's existing
    # chromium-usable signal (scripts/viewport-snapshot.sh). Probed once per
    # heartbeat; surfaced in the isolation_backends inventory so the qualifier's
    # walkthrough site gate admits only a host proven for Chromium, and fails
    # closed when unprovable (absent browser is never defaulted to True).
    playwright_chromium_usable: bool = False

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
        )

    def preferred_container(self) -> str | None:
        """Pick a container backend; docker/podman first (image-native), then HPC."""
        if self.docker_usable:
            return BACKEND_DOCKER
        if self.podman_usable:
            return BACKEND_PODMAN
        if self.apptainer_path:
            return BACKEND_APPTAINER
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


_GPU_PROBE_IMAGE = "nvidia/cuda:12.6.3-base-ubuntu24.04"
_GPU_PROBE_IMAGE_ENV = "BLUEPRINT_RUNNER_GPU_PROBE_IMAGE"


def _gpu_probe_image(image: str | None = None) -> str:
    """Return an explicitly supplied or operator-configured probe image.

    The default public CUDA image remains useful for a stock runner.  Runners
    behind a registry egress policy can instead configure a project image they
    can already pull (and which includes ``nvidia-smi``).  Resolve this at each
    probe rather than at import/startup so a transient failed proof can recover
    after an operator changes the runner service configuration.
    """
    if image:
        return image
    return os.environ.get(_GPU_PROBE_IMAGE_ENV, "").strip() or _GPU_PROBE_IMAGE


def probe_container_gpu_passthrough(
    caps: Capabilities,
    *,
    image: str | None = None,
    timeout: int = 45,
) -> dict[str, dict[str, object]]:
    """Functionally verify GPU passthrough for the selected container runtime.

    The probe uses the same argv builders and GPU flag mode as a real compute
    job, is bounded, and forwards no runner/orchestrator credentials.  It uses
    an explicit image or ``BLUEPRINT_RUNNER_GPU_PROBE_IMAGE`` when configured,
    allowing registry-restricted runners to prove their project image path.
    Only the preferred runtime is probed because that is the runtime the job
    planner will actually execute.  A missing GPU, timeout, launch error, empty
    GPU listing, or non-zero result is explicit negative evidence rather than
    inferred capability.
    """
    runtime = caps.preferred_container()
    if runtime is None:
        return {}
    mode = "cdi" if runtime == BACKEND_DOCKER and caps.docker_cdi_gpu else (
        "nv" if runtime == BACKEND_APPTAINER else "hook"
    )
    if not caps.gpu_indices:
        return {runtime: {"verified": False, "mode": mode, "reason": "no_host_gpu"}}
    image = _gpu_probe_image(image)
    # This probe runs before stale-container recovery.  Make its name unique to
    # this daemon invocation so a probe left by a crashed predecessor cannot
    # turn a name collision into negative capability evidence for this process.
    probe_container_name = (
        f"bp-gpu-passthrough-probe-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        if runtime in (BACKEND_DOCKER, BACKEND_PODMAN)
        else None
    )
    client_env = {
        key: value
        for key in (
            "PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG",
            "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY", "XDG_RUNTIME_DIR",
        )
        if (value := os.environ.get(key))
    }
    try:
        if runtime in (BACKEND_DOCKER, BACKEND_PODMAN):
            argv = build_docker_argv(
                image=image,
                command="nvidia-smi -L",
                container_name=probe_container_name,
                gpu_indices=[caps.gpu_indices[0]],
                binds=[],
                env_keys=[],
                cwd=None,
                runtime=runtime,
                gpu_cdi=(runtime == BACKEND_DOCKER and caps.docker_cdi_gpu),
            )
        elif runtime == BACKEND_APPTAINER and caps.apptainer_path:
            argv = build_apptainer_argv(
                apptainer_path=caps.apptainer_path,
                image=image,
                command="nvidia-smi -L",
                gpu=True,
                binds=[],
                cwd=None,
            )
        else:
            return {runtime: {"verified": False, "mode": mode, "reason": "unsupported_runtime"}}
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=client_env,
        )
    except subprocess.TimeoutExpired:
        _force_remove_container(runtime, probe_container_name, client_env)
        return {runtime: {"verified": False, "mode": mode, "reason": "probe_timeout"}}
    except (OSError, subprocess.SubprocessError):
        return {runtime: {"verified": False, "mode": mode, "reason": "probe_launch_failed"}}
    verified = completed.returncode == 0 and "GPU " in (completed.stdout or "")
    if verified:
        reason = "verified"
    elif completed.returncode == 0:
        reason = "no_gpu_visible"
    else:
        reason = f"probe_exit_{completed.returncode}"
    return {runtime: {"verified": verified, "mode": mode, "reason": reason}}


def container_gpu_passthrough_needs_retry(caps: Capabilities) -> bool:
    """Return whether a failed container GPU proof may recover without restart.

    A missing host GPU is stable for this daemon's detected hardware snapshot;
    retrying it just burns a container launch on every heartbeat. Runtime launch,
    pull, and hook failures are deliberately retried because those depend on
    recoverable container-store and registry state.
    """
    runtime = caps.preferred_container()
    if runtime is None:
        return False
    report = caps.container_gpu_passthrough.get(runtime)
    if not isinstance(report, dict) or report.get("verified") is True:
        return False
    return report.get("reason") not in {"no_host_gpu", "unsupported_runtime"}


def refresh_container_gpu_passthrough(caps: Capabilities) -> Capabilities:
    """Re-probe only the cached container GPU proof, preserving host capability facts."""
    return replace(
        caps,
        container_gpu_passthrough=probe_container_gpu_passthrough(caps),
    )


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


# Known LLM CLI binaries the runner may advertise in the heartbeat.
_LLM_CLI_NAMES: tuple[str, ...] = ("claude", "codex", "gemini")
MIN_CODEX_CLI_VERSION: tuple[int, int, int] = (0, 144, 1)
CLAUDE_RESIDENT_REQUIRED_FLAGS: frozenset[str] = frozenset(
    {
        "--input-format",
        "--append-system-prompt",
        "--mcp-config",
        "--strict-mcp-config",
        "--allowedTools",
    }
)


def _with_claude_resident_capabilities(
    info: dict,
    completed: subprocess.CompletedProcess | None,
    *,
    failure_reason: str | None = None,
) -> dict:
    enriched = dict(info)
    if completed is None or completed.returncode != 0:
        enriched.update(
            {
                "resident_ready": False,
                "missing_resident_flags": sorted(CLAUDE_RESIDENT_REQUIRED_FLAGS),
                "capability_probe_reason": failure_reason or "help_probe_failed",
            }
        )
        return enriched
    output = "\n".join((completed.stdout or "", completed.stderr or ""))
    flags = set(re.findall(r"--[0-9A-Za-z][0-9A-Za-z-]*", output))
    missing = sorted(CLAUDE_RESIDENT_REQUIRED_FLAGS - flags)
    enriched.update(
        {
            "resident_ready": not missing,
            "missing_resident_flags": missing,
            "capability_probe_reason": None if not missing else "missing_required_flags",
        }
    )
    return enriched


def _llm_cli_probe_reason(name: str, output: str) -> str:
    """Return a stable, non-secret reason for a failed CLI startup probe."""
    lowered = output.lower()
    if name == "codex" and (
        "missing optional dependency @openai/codex-" in lowered
        or "could not find package @openai/codex-" in lowered
    ):
        return "missing_native_package"
    return "version_probe_failed"


def _llm_cli_result(
    name: str,
    completed: subprocess.CompletedProcess,
    *,
    executable_found: bool = True,
) -> dict:
    """Convert a quota-free ``--version`` subprocess result to capability state."""
    version = ""
    healthy = False
    reason: str | None = None
    if completed.returncode == 0:
        version = (completed.stdout or completed.stderr or "").strip()
        healthy = bool(version)
        if not healthy:
            reason = "empty_version_output"
        elif name == "codex":
            match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version)
            if not match:
                healthy = False
                reason = "unparseable_version"
            elif tuple(int(part) for part in match.groups()) < MIN_CODEX_CLI_VERSION:
                healthy = False
                reason = "unsupported_version"
    else:
        output = "\n".join((completed.stdout or "", completed.stderr or ""))
        reason = _llm_cli_probe_reason(name, output)
    return {
        "present": healthy,
        "executable_found": executable_found,
        "healthy": healthy,
        "version": version,
        "reason": reason,
    }


def _unavailable_llm_cli(reason: str, *, executable_found: bool = False) -> dict:
    return {
        "present": False,
        "executable_found": executable_found,
        "healthy": False,
        "version": "",
        "reason": reason,
    }


def probe_llm_clis(*, env: dict[str, str] | None = None) -> dict[str, dict]:
    """Probe known LLM CLIs and return eligibility plus installation state.

    A PATH entry alone is not enough: npm can leave the Codex JavaScript shim in
    place while its platform-native optional package is missing. ``present``
    therefore means *runnable and dispatch-eligible*, not merely found by
    ``shutil.which``. ``executable_found`` preserves the latter distinction,
    and ``healthy`` is the explicit signal understood by newer consumers.

    ``<binary> --version`` exercises provider CLI startup without making an API
    request or consuming model quota. A non-zero/empty/exception result fails
    closed. This also catches Codex's concrete
    ``Missing optional dependency @openai/codex-linux-x64`` failure.

    Returns a dict keyed on the canonical CLI name::

        {"claude": {"present": True, "executable_found": True,
                    "healthy": True, "version": "0.2.0", "reason": None},
         "codex": {"present": False, "executable_found": True,
                   "healthy": False, "version": "",
                   "reason": "missing_native_package"}}

    ``present`` retains its legacy field name so today's server-side selector
    immediately excludes a newly reported unhealthy CLI. Older heartbeat
    payloads that contain only ``present``/``version`` remain valid.
    """
    result: dict[str, dict] = {}
    for name in _LLM_CLI_NAMES:
        path = shutil.which(name, path=(env or {}).get("PATH")) if env else _which(name)
        if path is None:
            result[name] = _unavailable_llm_cli("not_found")
            if name == "claude":
                result[name] = _with_claude_resident_capabilities(
                    result[name], None, failure_reason="not_found"
                )
            continue
        try:
            ver = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                env=env,
            )
            result[name] = _llm_cli_result(name, ver)
            if name == "claude":
                if result[name].get("healthy") is not True:
                    result[name] = _with_claude_resident_capabilities(
                        result[name], None, failure_reason=result[name].get("reason")
                    )
                else:
                    try:
                        help_result = subprocess.run(
                            [path, "--help"],
                            capture_output=True,
                            text=True,
                            timeout=10,
                            env=env,
                        )
                        result[name] = _with_claude_resident_capabilities(
                            result[name], help_result
                        )
                    except subprocess.TimeoutExpired:
                        result[name] = _with_claude_resident_capabilities(
                            result[name], None, failure_reason="help_probe_timeout"
                        )
                    except (OSError, subprocess.SubprocessError):
                        result[name] = _with_claude_resident_capabilities(
                            result[name], None, failure_reason="help_probe_error"
                        )
        except subprocess.TimeoutExpired:
            result[name] = _unavailable_llm_cli(
                "version_probe_timeout", executable_found=True
            )
            if name == "claude":
                result[name] = _with_claude_resident_capabilities(
                    result[name], None, failure_reason="version_probe_timeout"
                )
        except (OSError, subprocess.SubprocessError):
            result[name] = _unavailable_llm_cli(
                "version_probe_error", executable_found=True
            )
            if name == "claude":
                result[name] = _with_claude_resident_capabilities(
                    result[name], None, failure_reason="version_probe_error"
                )
    return result


def probe_container_llm_clis(
    image: str,
    *,
    runtime: str = BACKEND_DOCKER,
) -> dict[str, dict]:
    """Probe CLIs inside the selected agent image without calling a provider API.

    The image's entrypoint is replaced with each CLI and only ``--version`` is
    executed. This catches both directions of host/image drift: a healthy host
    must not mask a broken image, and a healthy image must not be disabled by a
    host that intentionally has no provider CLIs.
    """
    if not image:
        return {
            name: _unavailable_llm_cli("image_not_configured")
            for name in _LLM_CLI_NAMES
        }
    result: dict[str, dict] = {}
    for name in _LLM_CLI_NAMES:
        try:
            completed = subprocess.run(
                [
                    runtime,
                    "run",
                    "--rm",
                    "--entrypoint",
                    name,
                    image,
                    "--version",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            output = "\n".join(
                (completed.stdout or "", completed.stderr or "")
            ).lower()
            missing_entrypoint = completed.returncode in (125, 126, 127) and any(
                marker in output
                for marker in (
                    "executable file not found",
                    "executable not found",
                    "command not found",
                    "no such file or directory",
                )
            )
            if missing_entrypoint:
                result[name] = _unavailable_llm_cli("not_found")
            else:
                result[name] = _llm_cli_result(name, completed)
                if name == "claude":
                    if result[name].get("healthy") is not True:
                        result[name] = _with_claude_resident_capabilities(
                            result[name], None, failure_reason=result[name].get("reason")
                        )
                    else:
                        try:
                            help_result = subprocess.run(
                                [
                                    runtime,
                                    "run",
                                    "--rm",
                                    "--entrypoint",
                                    name,
                                    image,
                                    "--help",
                                ],
                                capture_output=True,
                                text=True,
                                timeout=20,
                            )
                            result[name] = _with_claude_resident_capabilities(
                                result[name], help_result
                            )
                        except subprocess.TimeoutExpired:
                            result[name] = _with_claude_resident_capabilities(
                                result[name], None,
                                failure_reason="container_help_probe_timeout",
                            )
                        except (OSError, subprocess.SubprocessError):
                            result[name] = _with_claude_resident_capabilities(
                                result[name], None,
                                failure_reason="container_help_probe_error",
                            )
        except subprocess.TimeoutExpired:
            result[name] = _unavailable_llm_cli(
                "version_probe_timeout", executable_found=True
            )
            if name == "claude":
                result[name] = _with_claude_resident_capabilities(
                    result[name], None, failure_reason="container_probe_timeout"
                )
        except (OSError, subprocess.SubprocessError):
            result[name] = _unavailable_llm_cli("container_probe_error")
            if name == "claude":
                result[name] = _with_claude_resident_capabilities(
                    result[name], None, failure_reason="container_probe_error"
                )
    return result


def playwright_cache_dir() -> str:
    """The Playwright browser-cache path this runner serves to agent sandboxes.

    Single source of truth shared by the capability probe
    (:func:`probe_playwright_chromium`) and the bwrap builder
    (:func:`build_bwrap_argv`): the path the gate certifies is EXACTLY the path
    bound read-only into the agent sandbox at ``~/.cache/ms-playwright``, so the
    admitted capability is true WHERE the walkthrough harness consumes Chromium
    (inside the bwrap sandbox), not merely where it is measured (the runner host).
    """
    return os.path.expanduser(os.path.join("~", ".cache", "ms-playwright"))


def probe_playwright_chromium() -> bool:
    """True when the host has a Playwright-managed Chromium browser build.

    Mirrors the project's existing chromium-usable signal
    (``scripts/viewport-snapshot.sh``): the Playwright browsers cache contains a
    ``chromium-*`` directory. A missing cache is unprovable → False (fail-closed:
    a host that cannot serve Chromium to the walkthrough harness never claims the
    candidate-stack site). The path probed is :func:`playwright_cache_dir` — the
    same path :func:`build_bwrap_argv` mounts into the sandbox, so the claim the
    gate accepts is the path the harness can actually reach.
    """
    cache = playwright_cache_dir()
    try:
        if not os.path.isdir(cache):
            return False
        return any(
            name.startswith("chromium-") and os.path.isdir(os.path.join(cache, name))
            for name in os.listdir(cache)
        )
    except OSError:
        return False


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
    # Enroot is intentionally not advertised: the execution planner has no real
    # Enroot argv path.  Treating binary presence as support routed it through
    # Apptainer and could crash before launch.
    enroot_path = None
    systemd_run_path = probe_systemd_run()
    playwright_chromium_usable = probe_playwright_chromium()
    gpu_indices, gpu_names = detect_gpus()

    notes: list[str] = []
    if docker_present and not docker_usable:
        notes.append("docker present but 'docker info' failed (socket denied?) — unusable")
    if bwrap_path and not userns_ok:
        notes.append("bwrap present but unprivileged userns unavailable — bwrap unusable")

    caps = Capabilities(
        bwrap_path=bwrap_path,
        unshare_path=unshare_path,
        userns_ok=userns_ok,
        docker_usable=docker_usable,
        docker_cdi_gpu=docker_cdi_gpu,
        podman_usable=podman_usable,
        apptainer_path=apptainer_path,
        enroot_path=enroot_path,
        systemd_run_path=systemd_run_path,
        playwright_chromium_usable=playwright_chromium_usable,
        gpu_indices=gpu_indices,
        gpu_names=gpu_names,
        notes=tuple(notes),
    )
    return replace(
        caps,
        container_gpu_passthrough=probe_container_gpu_passthrough(caps),
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

    Backends, in decision order (the order is load-bearing — see the opt-in note
    in the body: checking bwrap first silently neuters the opt-in):

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
    # Documented necessity #1 — the prod-parity OPT-IN, and it must be checked
    # BEFORE bwrap or it can never fire.
    #
    # `agent_image` is not incidental: blueprint_runner only resolves one when the
    # operator explicitly asks (`--docker-agent`, BLUEPRINT_AGENT_BACKEND=docker,
    # or `--pull`), and leaves it None otherwise. So a truthy agent_image IS the
    # opt-in signal. Testing bwrap first made that flag a SILENT NO-OP on every
    # host with bwrap — the operator asked for a prod-parity container run and
    # quietly got bwrap instead, with nothing said. An opt-in that does nothing
    # when you opt in is worse than no opt-in at all.
    #
    # Still gated by task type: the write/heavy slice never rides the container
    # path, regardless of the flag.
    if (
        agent_image
        and caps.docker_usable
        and (task_type is None or task_type in AGENT_DOCKER_TYPES)
    ):
        return BACKEND_DOCKER
    # Preferred default: rootless bwrap when binary + sandbox are both available.
    # With no opt-in (agent_image falsy) the docker branch above cannot fire, so
    # this preserves the historical bwrap-or-decline table byte for byte.
    if agent_binary_present and caps.bwrap_ok:
        return BACKEND_BWRAP
    return DECLINE


def agent_decline_reason(caps: Capabilities, agent_binary_present: bool) -> str | None:
    if not agent_binary_present:
        return "agent binary not built (run: cd agent && npm run build)"
    if not caps.bwrap_ok:
        return "no bwrap sandbox (install bubblewrap; kernel userns required)"
    return None


def decide_compute_backend(
    caps: Capabilities, has_image: bool, *, allow_host_fallback: bool = False
) -> str:
    """Choose a compute backend, fail-closed.

    Table (image? × runtime? × bwrap? × allow_host_fallback?):
      image + container-runtime                        → container backend (docker/podman/apptainer/enroot)
      image + no runtime + bwrap + allow_host_fallback → bwrap (explicit opt-in; image unfulfillable)
      image + no runtime + bwrap                       → DECLINE (default; honest refusal)
      no image + bwrap                                 → bwrap
      no image + no bwrap                              → DECLINE
      image + no runtime + no bwrap                    → DECLINE
    """
    if has_image and caps.any_container_runtime():
        return caps.preferred_container() or DECLINE
    if caps.bwrap_ok:
        if has_image and not allow_host_fallback:
            return DECLINE
        return BACKEND_BWRAP
    return DECLINE


def compute_decline_reason(
    caps: Capabilities, has_image: bool, *, allow_host_fallback: bool = False
) -> str | None:
    backend = decide_compute_backend(caps, has_image, allow_host_fallback=allow_host_fallback)
    if backend != DECLINE:
        return None
    if has_image:
        if caps.bwrap_ok and not allow_host_fallback:
            return (
                "container_image requested but no usable container runtime — runner declined "
                "(set allow_host_fallback=true in the launch contract to permit imageless bwrap fallback)"
            )
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
        "enroot_ok": False,
        "systemd_run_ok": bool(caps.systemd_run_path),
        # Runner-emitted Chromium proof for the qualify-time walkthrough site gate
        # (the host's Playwright browser cache). Fail-closed: absent → False.
        "playwright_chromium_usable": caps.playwright_chromium_usable,
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

    When ``per_task_creds`` is provided (server-vended per-task credentials), it
    replaces matching keys from source_env. This is the single credential path:
    no fallback to static host env vars for vended credential keys. Speech keys
    are intentionally *not* in ``AGENT_ENV_ALLOWLIST``, so they can arrive only
    in ``per_task_creds`` and never flow from a long-lived runner environment.
    Both injected and vended values are filtered through fixed allowlists.
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
    env.update(
        {
            key: value
            for key, value in injected.items()
            if key in _AGENT_INJECTED_ENV_ALLOWLIST
            and isinstance(value, str)
            and value
        }
    )
    if per_task_creds:
        for key, val in per_task_creds.items():
            if (
                key in SERVER_VENDED_AGENT_ENV_ALLOWLIST
                and isinstance(val, str)
                and val
            ):
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

    No provider or Resident speech credentials (untrusted experiment code never
    sees those keys) and never a full os.environ copy. ``injected`` =
    BLUEPRINT_RESULT_FILE, BLUEPRINT_ARTIFACT_DIR, BLUEPRINT_PROGRESS_FILE,
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
    for forbidden in COMPUTE_FORBIDDEN_ENV_KEYS:
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

# Default host root for the shared pip/uv package store. Mirrors the agent-side
# default in agent/src/packageStore.ts (DEFAULT_PACKAGE_STORE_ROOT).
PACKAGE_STORE_DEFAULT_ROOT = "/opt/agent-cache"


def package_store_dirs(source_env: dict[str, str]) -> list[str]:
    """Host dirs backing the shared pip/uv package store.

    The store is content-addressed: pip/uv stage downloads into a temp file then
    atomically rename into place, so concurrent workspaces sharing the same dirs
    is safe. Returns the ``pip``/``uv`` subdirs under the configured root (or the
    baked ``/opt/agent-cache`` default) for RW-binding into a bwrap sandbox.
    """
    root = source_env.get("BLUEPRINT_PACKAGE_STORE_ROOT") or PACKAGE_STORE_DEFAULT_ROOT
    return [os.path.join(root, "pip"), os.path.join(root, "uv")]


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
    playwright_cache: str | None = None,
    rw_binds: list[str] | None = None,
) -> list[str]:
    """Assemble a rootless bwrap invocation.

    Guarantees (asserted in tests):
      * Namespaces: user/ipc/pid/uts unshared, cgroup best-effort; --die-with-parent
        and --new-session so the whole tree dies with the runner and detaches TTY.
      * Fresh tmpfs /home and /tmp — NO host $HOME is ever bound, EXCEPT the
        Playwright browser cache when ``playwright_cache`` is supplied: that one
        read-only subpath is layered onto the tmpfs home at the exact path the
        walkthrough harness's ``chromium.launch()`` reads, so the runner-emitted
        ``playwright_chromium_usable`` capability is true WHERE it is consumed.
      * Read-only minimal toolchain (/usr,/bin,/lib,... via --ro-bind-try) + the
        resolved tool paths; the per-task workspace bound RW at /workspace.
      * ``rw_binds`` are bound READ-WRITE at the same path (--bind-try, skipped
        when the host dir is absent). The shared pip/uv package store relies on
        this so a workspace's pip/uv can populate a cache the host image baked
        (content-addressed, safe for concurrent workspaces).
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

    # The walkthrough harness launches Playwright Chromium INSIDE the sandbox;
    # the browser build lives in the runner's Playwright cache (the same path
    # probe_playwright_chromium certifies). Bind it read-only at the sandbox
    # HOME path Playwright reads so the admitted capability is true where the
    # harness consumes it. --ro-bind-try layers onto the tmpfs /home above and
    # skips a host that has no cache, so non-browser sandboxes are unchanged.
    if playwright_cache and os.path.isdir(playwright_cache):
        argv += [
            "--ro-bind-try",
            playwright_cache,
            os.path.join(SANDBOX_HOME, ".cache", "ms-playwright"),
        ]

    # Read-only toolchain / config. --ro-bind-try skips paths absent on this host.
    seen_src: set[str] = set()
    # A tool already covered by a directory bind must not be mounted again at
    # its individual path.  In particular, bind-mounting an npm ``bin`` symlink
    # dereferences it into a regular file.  Node then resolves modules relative
    # to the shim location instead of the package entry point and cannot find
    # sibling optional native packages.  Keeping the broader directory mount
    # preserves the symlink exactly as installed on the host.
    covering_roots = tuple(src for src in [*ro, *libs] if src)
    uncovered_tools = [
        path for path in tools if not _covered_by(path, covering_roots)
    ]
    for src in [*ro, *uncovered_tools, *libs]:
        if src and src not in seen_src:
            seen_src.add(src)
            argv += ["--ro-bind-try", src, src]

    # Shared caches RW (pip/uv package store). --bind-try skips a host dir that
    # does not exist (e.g. a local runner without the baked image dirs), leaving
    # the sandbox with no shared store — the agent degrades to per-workspace
    # caches. Dedup against RO binds so a path is never mounted twice.
    for src in (rw_binds or []):
        if src and src not in seen_src:
            seen_src.add(src)
            argv += ["--bind-try", src, src]

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


def npm_tool_roots(paths: list[str]) -> list[str]:
    """Package roots needed by npm-managed CLIs inside bwrap.

    System-global CLIs normally live under an existing /usr or /opt base bind.
    Blueprint-managed CLIs live in a private runner directory, so binding only
    the shim/real entry file would omit sibling JS and native optional packages.
    Bind the containing release root read-only when a resolved tool came through
    ``node_modules/.bin``.  Global npm prefixes use ``<prefix>/bin/<tool>``
    symlinks whose real path is under ``<prefix>/lib/node_modules/<package>``.
    For a scoped global package, bind the whole scope directory rather than
    only the package root: npm may install platform optional dependencies as
    siblings (notably ``@openai/codex`` and ``@openai/codex-linux-*``).  Also
    bind a global prefix's ``bin`` directory so its package shims remain
    symlinks inside bwrap instead of being dereferenced by an individual bind.
    """
    roots: list[str] = []
    global_npm_prefixes: list[str] = []
    for path in paths:
        bin_dir = os.path.dirname(path)
        node_modules = os.path.dirname(bin_dir)
        if (
            os.path.basename(bin_dir) == ".bin"
            and os.path.basename(node_modules) == "node_modules"
        ):
            root = os.path.dirname(node_modules)
            if root and root not in roots:
                roots.append(root)

        # A resolved global npm shim points at the real package entry file, for
        # example:
        #   /home/runner/node22/lib/node_modules/@openai/codex/bin/codex.js
        # Binding only that file leaves sibling optional native dependencies
        # outside bwrap.  For scoped packages capture the scope root; for
        # unscoped packages capture the package root.  A broader managed-release
        # root discovered through node_modules/.bin still wins below.
        normalized = path.replace("\\", "/")
        match = re.match(
            r"^(.*?/node_modules/)(@[^/]+/)?([^/]+)(?:/|$)", normalized
        )
        if match:
            node_modules_root = match.group(1).rstrip("/")
            global_suffix = "/lib/node_modules"
            if node_modules_root.endswith(global_suffix):
                prefix = node_modules_root[: -len(global_suffix)]
                if prefix and prefix not in global_npm_prefixes:
                    global_npm_prefixes.append(prefix)
            scope = (match.group(2) or "").rstrip("/")
            bind_root = (
                f"{node_modules_root}/{scope}"
                if scope
                else f"{node_modules_root}/{match.group(3)}"
            )
            if bind_root and bind_root not in roots:
                roots.append(bind_root)

    # Global npm exposes package executables through <prefix>/bin symlinks.
    # Bind that directory alongside the package root so build_bwrap_argv can
    # omit the individual shim bind and preserve Node's real module location.
    for path in paths:
        parent = posixpath.dirname(path.replace("\\", "/"))
        for prefix in global_npm_prefixes:
            bin_root = f"{prefix}/bin"
            if parent == bin_root and bin_root not in roots:
                roots.append(bin_root)

    # Prefer a broader release root over a nested package root when both were
    # discovered from the real entry file plus its node_modules/.bin shim.
    return [
        root
        for root in roots
        if not any(
            other != root and _covered_by(root, (other,)) for other in roots
        )
    ]


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

    ``per_task_creds`` = server-vended per-task credential env vars (optional).
    It may include the Resident-only speech keys; those keys cannot come from
    ``source_env``. When provided, these replace matching keys from source_env
    AND injected_env. Non-credential keys (LANG, TERM, PATH,
    NODE_EXTRA_CA_CERTS, etc.) still flow from source_env. Applied to both bwrap
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
        forwarded.update(
            {
                key: value
                for key, value in injected_env.items()
                if key in _AGENT_INJECTED_ENV_ALLOWLIST
                and isinstance(value, str)
                and value
            }
        )
        if per_task_creds:
            for key, val in per_task_creds.items():
                if (
                    key in SERVER_VENDED_AGENT_ENV_ALLOWLIST
                    and isinstance(val, str)
                    and val
                ):
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
    ro_binds = [
        *_BASE_RO_BINDS,
        agent_pkg_root,
        agent_real,
        PROJECT_VERIFICATION_VENV,
        *npm_tool_roots(tools),
    ]

    env = build_agent_env(
        source_env,
        {
            **injected_env,
            # Point the agent at a subdirectory of the bind mount, not the
            # mountpoint itself: rm -rf on the mountpoint returns EBUSY.
            "AGENT_WORKSPACE": SANDBOX_WORKSPACE + "/agent",
            "HOME": SANDBOX_HOME,
        },
        [os.path.join(PROJECT_VERIFICATION_VENV, "bin"), *tool_dirs_for(tools)],
        per_task_creds=per_task_creds,
    )

    argv = build_bwrap_argv(
        bwrap_path=caps.bwrap_path,
        command_argv=agent_binary,
        workspace_host=workspace_host,
        ro_binds=ro_binds,
        tool_paths=tools,
        # Expose the runner's Playwright browser cache read-only at the sandbox
        # path the walkthrough harness reads (skipped when absent) so the
        # chromium capability the gate admits is true where it is consumed.
        playwright_cache=playwright_cache_dir(),
        # Shared pip/uv package store, RW: workspace pip/uv writes into the
        # content-addressed host cache (atomic rename → safe for concurrent
        # workspaces) instead of re-downloading per workspace. Skipped when the
        # host dirs are absent; the agent then falls back to per-workspace caches.
        rw_binds=package_store_dirs(source_env),
    )
    return ExecutionPlan(backend=backend, argv=argv, env=env)


def _rel_to_sandbox(host_path: str | None, workspace_host: str, mount: str) -> str | None:
    """Translate a host path under ``workspace_host`` to its in-sandbox path."""
    if not host_path:
        return None
    rel = os.path.relpath(host_path, workspace_host)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
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
    progress_file_host: str | None = None,
    clone_dir_host: str | None,
    cwd_host: str | None,
    job_env: dict[str, str],
    gpu_indices: list[int],
    container_name: str,
    source_env: dict[str, str],
    ldconfig_output: str = "",
    cpu_limit: str | None = None,
    mem_limit: str | None = None,
    allow_host_fallback: bool = False,
) -> ExecutionPlan:
    """Build the sandbox plan for a compute job, or DECLINE (fail-closed).

    All ``*_host`` paths are on the runner filesystem; result_file/artifact_dir/
    progress_file/clone_dir are expected to live UNDER ``workspace_host`` (the
    per-job tmpdir). ``progress_file_host`` is optional: when set, every backend
    injects ``BLUEPRINT_PROGRESS_FILE`` (append-only JSONL the experiment may
    write interim progress records to); when None the env var is simply absent —
    byte-identical to the pre-progress behavior.

    When ``image`` is set but no container runtime is available, the job is DECLINED
    by default.  Set ``allow_host_fallback=True`` in the launch contract to permit
    an explicit imageless bwrap fallback (reproducibility trade-off; operator opt-in).
    """
    has_image = bool(image)
    backend = decide_compute_backend(caps, has_image, allow_host_fallback=allow_host_fallback)
    if backend == DECLINE:
        return ExecutionPlan.decline(
            compute_decline_reason(caps, has_image, allow_host_fallback=allow_host_fallback)
            or "no compute sandbox"
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
        if progress_file_host:
            # Interim-progress channel (append-only JSONL; the runner tails it
            # between heartbeats). Mirrors BLUEPRINT_RESULT_FILE path translation.
            inner["BLUEPRINT_PROGRESS_FILE"] = (
                _rel_to_sandbox(progress_file_host, workspace_host, out_mount)
                or f"{out_mount}/progress.jsonl"
            )
        if clone_dir_host:
            binds.append((clone_dir_host, "/workspace"))
            inner["PYTHONPATH"] = "/workspace"
            # The clone is owned by the runner's host UID but the container
            # user (root in NGC images) differs, so git inside the container
            # refuses every command with "dubious ownership". Injected via env
            # rather than writing /workspace/.gitconfig — a written file would
            # show up as an untracked path and dirty the very checkout that
            # strict-cleanliness experiment scripts are verifying.
            inner["GIT_CONFIG_COUNT"] = "1"
            inner["GIT_CONFIG_KEY_0"] = "safe.directory"
            inner["GIT_CONFIG_VALUE_0"] = "/workspace"
        inner.update({k: str(v) for k, v in job_env.items()})
        for forbidden in FORBIDDEN_ENV_KEYS:
            inner.pop(forbidden, None)
        cwd_in = (
            _rel_to_sandbox(cwd_host, clone_dir_host, "/workspace")
            if clone_dir_host and cwd_host
            else ("/workspace" if clone_dir_host else cwd_host)
        )
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

    # ---- apptainer (image, HPC-style) ----------------------------------------
    if backend == BACKEND_APPTAINER:
        assert caps.apptainer_path is not None
        result_in = _rel_to_sandbox(result_file_host, workspace_host, "/workspace")
        artifacts_in = _rel_to_sandbox(artifact_dir_host, workspace_host, "/workspace")
        binds = [(workspace_host, "/workspace")]
        inner = {
            "BLUEPRINT_RESULT_FILE": result_in or "/workspace/result.json",
            "BLUEPRINT_ARTIFACT_DIR": artifacts_in or "/workspace/artifacts",
            "HOME": "/workspace",
        }
        if progress_file_host:
            inner["BLUEPRINT_PROGRESS_FILE"] = (
                _rel_to_sandbox(progress_file_host, workspace_host, "/workspace")
                or "/workspace/progress.jsonl"
            )
        clone_in = _rel_to_sandbox(clone_dir_host, workspace_host, "/workspace")
        if clone_in:
            inner["PYTHONPATH"] = clone_in
        if gpu_indices:
            inner["CUDA_VISIBLE_DEVICES"] = ",".join(str(i) for i in gpu_indices)
            inner["NVIDIA_VISIBLE_DEVICES"] = ",".join(str(i) for i in gpu_indices)
        inner.update({k: str(v) for k, v in job_env.items()})
        for forbidden in FORBIDDEN_ENV_KEYS:
            inner.pop(forbidden, None)
        cwd_in = (
            _rel_to_sandbox(cwd_host, workspace_host, "/workspace")
            if cwd_host
            else (clone_in or "/workspace")
        )
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
    if progress_file_host:
        injected["BLUEPRINT_PROGRESS_FILE"] = (
            _rel_to_sandbox(progress_file_host, workspace_host, SANDBOX_WORKSPACE)
            or f"{SANDBOX_WORKSPACE}/progress.jsonl"
        )
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
    cwd_in = (
        _rel_to_sandbox(cwd_host, workspace_host, SANDBOX_WORKSPACE)
        if cwd_host
        else (clone_in or SANDBOX_WORKSPACE)
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


# Fatal "no usable GPU" diagnostics printed by container entrypoints (notably
# the NGC / ``nvcr.io/nvidia/pytorch`` images) when the host GPU predates the
# image's minimum CUDA compute capability — e.g. a Tesla V100 (Volta, CC 7.0)
# under a container built for Turing+.
#
# These are the ONLY pre-run signal for an arch mismatch: ``nvidia-smi -L``
# queries the DRIVER, which lists the V100 happily regardless of what the
# image's CUDA runtime can actually use, and the entrypoint prints the banner
# and still exits 0. Exit code and device visibility both look healthy.
#
# MUST stay byte-identical to ``_NO_USABLE_GPU_LOG_MARKERS`` in
# ``backend/app/compute_jobs.py`` (the reactive post-run guard that reclassifies
# a SUCCEEDED job on the same markers). ``scripts/`` is deployed standalone on
# runner hosts with no ``backend`` package importable, so the tuple is
# DUPLICATED rather than imported; parity is enforced by
# ``backend/testing/test_runner_isolation_gpu_markers.py``, which fails CI if
# either list is edited without the other.
NO_USABLE_GPU_LOG_MARKERS: tuple[str, ...] = (
    "no supported gpu(s) detected to run this container",
    "gpu, which is not supported by this container",
    # CUDA error 209: image lacks device code for host GPU arch (e.g. sm_70/Volta).
    # Added to match backend/app/compute_jobs.py::_NO_USABLE_GPU_LOG_MARKERS — parity
    # enforced by backend/testing/test_runner_isolation_gpu_markers.py.
    "no kernel image is available for execution on the device",
)

# Suffix distinguishing the probe container from the real run's container.
PROBE_NAME_SUFFIX = "-probe"


def no_usable_gpu_marker(text: str) -> str | None:
    """The first :data:`NO_USABLE_GPU_LOG_MARKERS` entry present in ``text``."""
    lowered = (text or "").lower()
    for marker in NO_USABLE_GPU_LOG_MARKERS:
        if marker in lowered:
            return marker
    return None


def _force_remove_container(
    runtime: str | None, name: str | None, env: dict[str, str] | None
) -> None:
    """Best-effort ``docker rm -f`` of a probe container orphaned by a timeout.

    ``--rm`` does NOT save us here: on :exc:`subprocess.TimeoutExpired` Python
    kills the docker CLI *client*, not the container. The container keeps
    running, ``--rm`` never fires, and the name stays held. Swallows every
    error — this is cleanup, never a failure path.
    """
    if not runtime or not name:
        return
    try:
        subprocess.run(
            [runtime, "rm", "-f", name],
            env=env,
            capture_output=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        pass


def pull_container_image(
    plan: ExecutionPlan, image: str | None, timeout: int = 1800
) -> tuple[bool, str]:
    """Pull *image* explicitly, so the GPU probe does not have to wait on it.

    Without this the probe's own ``docker run`` IS the pull — docker's default
    pull policy is ``missing`` — and the probe's short timeout would be racing a
    cold multi-GB download it cannot win. A 14-minute NGC pull against a 30s
    probe times out, and the job dies reporting a probe timeout instead of the
    arch mismatch the probe exists to name.

    Best-effort by design: a failed pull is NOT fatal here. ``docker run`` will
    re-attempt the pull anyway, so letting the probe (or the real run) surface
    the error keeps ONE authoritative failure path instead of inventing a second
    one. Returns ``(ok, detail)`` for logging only. Never raises.

    A no-op for imageless/bwrap plans, which have nothing to pull.
    """
    if not image or plan.backend not in (BACKEND_DOCKER, BACKEND_PODMAN):
        return True, "no image to pull"
    runtime = plan.argv[0] if plan.argv else None
    if not runtime:
        return True, "no container runtime in plan"
    try:
        result = subprocess.run(
            [runtime, "pull", image],
            env=plan.env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"image pull timed out after {timeout}s"
    except OSError as exc:
        return False, f"image pull failed to launch: {exc}"
    if result.returncode != 0:
        detail = ((result.stderr or "") + (result.stdout or "")).strip()
        return False, f"image pull exited {result.returncode}: {detail[:500]}"
    return True, f"pulled {image}"


def _project_verification_probe_result(
    plan: ExecutionPlan,
    probe_mode: str,
    *,
    timeout: int,
) -> dict[str, object]:
    """Run one verification leg in the exact planned agent boundary."""
    if plan.declined:
        return {
            "ok": False,
            "exit_code": None,
            "reason": plan.decline_reason or "agent boundary declined",
        }
    if plan.backend != BACKEND_BWRAP:
        return {
            "ok": False,
            "exit_code": None,
            "reason": f"backend {plan.backend!r} cannot provide authoring verification",
        }
    try:
        separator = len(plan.argv) - 1 - list(reversed(plan.argv)).index("--")
    except ValueError:
        return {
            "ok": False,
            "exit_code": None,
            "reason": "unexpected bwrap argv shape — cannot build verification probe",
        }
    probe_argv = list(plan.argv[: separator + 1]) + [
        "/bin/bash",
        PROJECT_VERIFICATION_SANDBOX_PATH,
        probe_mode,
    ]
    try:
        completed = subprocess.run(
            probe_argv,
            env=plan.env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "exit_code": None,
            "reason": f"{probe_mode} probe timed out after {timeout}s",
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "exit_code": None,
            "reason": f"{probe_mode} probe failed to launch: {str(exc)[:300]}",
        }

    raw_returncode = int(completed.returncode)
    # subprocess uses a negative return code to report signal termination.
    # Keep that as non-zero, shell-style failure evidence: clamping it to zero
    # would incorrectly earn verification readiness for a killed probe.
    exit_code = (
        min(255, 128 + abs(raw_returncode))
        if raw_returncode < 0
        else min(255, raw_returncode)
    )
    if exit_code == 0:
        return {"ok": True, "exit_code": 0, "reason": None}
    detail = " ".join(
        ((completed.stderr or "") + " " + (completed.stdout or "")).split()
    )[:400]
    return {
        "ok": False,
        "exit_code": exit_code,
        "reason": detail or f"{probe_mode} probe exited {exit_code}",
    }


def probe_project_verification(
    plan: ExecutionPlan,
    *,
    ruff_timeout: int = 30,
    docker_postgres_timeout: int = 90,
) -> dict[str, object]:
    """Earn versioned Ruff + live PostgreSQL evidence in an agent boundary.

    The plan is built for a write/authoring agent task.  Each functional probe
    reuses its bwrap prefix and environment verbatim, replacing only the terminal
    agent command. PostgreSQL starts inside the private sandbox from the baked
    toolchain; the host Docker daemon and its authority-bearing socket remain
    deliberately unreachable.
    """
    boundary = plan.backend if not plan.declined else DECLINE
    ruff = _project_verification_probe_result(
        plan, "ruff", timeout=ruff_timeout
    )
    docker_postgres = _project_verification_probe_result(
        plan, "docker-postgres", timeout=docker_postgres_timeout
    )
    ready = bool(
        ruff.get("ok") is True
        and ruff.get("exit_code") == 0
        and docker_postgres.get("ok") is True
        and docker_postgres.get("exit_code") == 0
    )
    failed_reasons = list(
        dict.fromkeys(
            str(result.get("reason"))
            for result in (ruff, docker_postgres)
            if result.get("reason")
        )
    )
    return {
        "version": PROJECT_VERIFICATION_VERSION,
        "boundary": {"class": CLASS_AGENT, "backend": boundary},
        "ready": ready,
        "reason": None if ready else "; ".join(failed_reasons)[:500],
        "probes": {
            "ruff": ruff,
            "docker_postgres": docker_postgres,
        },
    }


def probe_gpu_in_sandbox(plan: ExecutionPlan, timeout: int = 30) -> tuple[bool, str]:
    """Run ``nvidia-smi -L`` inside the EXACT sandbox the real job will use.

    The probe reuses the plan's argv/env verbatim, substituting only the
    terminal command argument (and, on container backends, the ``--name``) so
    the sandbox construction is identical (same bwrap bind mounts, same
    CUDA_VISIBLE_DEVICES, same /sys bind, same docker flags).

    Two independent failures are caught:

      * **Device invisibility** — ``nvidia-smi -L`` fails or lists no GPU, so
        the real job would silently fall back to CPU.
      * **Arch incompatibility** — the GPU IS listed (``nvidia-smi`` reads the
        driver, which is arch-agnostic) but the image's CUDA runtime cannot use
        it. The entrypoint says so on stderr and still exits 0, so only a marker
        scan of the COMBINED stdout+stderr can see it. Checking exit code and
        stdout alone lets a V100/Turing+-image mismatch through the gate, burn
        the image pull and the full run, and fail later as a confusing
        "no metric produced".

    Returns ``(True, output)`` when at least one GPU is listed and no
    no-usable-GPU marker appears; ``(False, reason)`` on any failure (non-zero
    exit, timeout, OSError, empty GPU list, or arch mismatch). Never raises.
    """
    probe_command = "nvidia-smi -L"
    probe_container: str | None = None
    runtime: str | None = None

    if plan.backend == BACKEND_BWRAP:
        # bwrap argv ends with: ... bash -lc <command>
        # Replace the trailing command string with the probe.
        if len(plan.argv) >= 3 and plan.argv[-2] == "-lc":
            probe_argv = list(plan.argv[:-1]) + [probe_command]
        else:
            return False, "unexpected bwrap argv shape — cannot build probe"
    elif plan.backend in (BACKEND_DOCKER, BACKEND_PODMAN):
        # docker/podman run … image bash -lc <command>
        # Replace everything after "bash -lc" with the probe.
        try:
            bash_idx = plan.argv.index("bash")
        except ValueError:
            return False, "unexpected docker argv shape — cannot build probe"
        probe_argv = list(plan.argv[:bash_idx]) + ["bash", "-lc", probe_command]
        runtime = probe_argv[0] if probe_argv else None
        # The probe MUST NOT reuse the real run's container name. A timed-out
        # probe leaks a still-running container holding that name (see
        # _force_remove_container), and the real run would then die with
        # "name already in use". A distinct name makes the collision
        # structurally impossible even if cleanup also fails.
        try:
            name_idx = probe_argv.index("--name")
        except ValueError:
            name_idx = -1
        if 0 <= name_idx < len(probe_argv) - 1:
            probe_container = probe_argv[name_idx + 1] + PROBE_NAME_SUFFIX
            probe_argv[name_idx + 1] = probe_container
    elif plan.backend in (BACKEND_APPTAINER, BACKEND_ENROOT):
        # apptainer exec … image <command>  →  replace terminal command
        probe_argv = list(plan.argv[:-1]) + [probe_command]
    else:
        return False, f"unsupported backend {plan.backend!r} for GPU probe"

    try:
        result = subprocess.run(
            probe_argv,
            env=plan.env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        _force_remove_container(runtime, probe_container, plan.env)
        return False, f"GPU probe timed out after {timeout}s"
    except OSError as exc:
        return False, f"GPU probe failed to launch: {exc}"

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    combined = (stdout + "\n" + stderr).strip()

    if result.returncode != 0:
        detail = combined or f"exit {result.returncode}"
        return False, f"nvidia-smi -L exited {result.returncode}: {detail}"

    # Arch gate runs BEFORE the device-visibility check: on a mismatch the GPU
    # list is populated and the exit code is 0, so the marker is the only
    # authoritative signal and must not be short-circuited by a healthy-looking
    # stdout.
    marker = no_usable_gpu_marker(combined)
    if marker:
        return False, (
            "container reported no usable GPU — host GPU/image compatibility "
            f"mismatch (matched {marker!r}): {combined}"
        )

    if not stdout:
        return False, "nvidia-smi -L returned no output"
    return True, stdout
