"""Agent VM lifecycle: provisioning, deletion, and local-mode subprocesses.

The compute layer the orchestrator and reaper call to bring agent runners up
and down. In GCP mode it provisions Compute Engine VMs — bounded by a
concurrency slot gate, with cross-region/zone fallback (``FALLBACK_ZONES``)
and classification of infrastructure-wide provision failures
(``INFRA_PROVISION_PATTERNS``) so transient outages don't burn task retry
budgets. When ``LOCAL_MODE`` is set, the same entry points
(``provision_agent_vm`` / ``delete_agent_vm``) run the agent as a local
subprocess instead. Per-VM boot secrets are built via ``compute_manifest``.
"""

import asyncio
import base64 as _base64
import json
import logging
import os
import random
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from collections import OrderedDict as _OrderedDict, deque as _deque
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Re-exported below for existing `from app.compute import ...` callers (and
# tests that mock via `app.compute.X`). Hoisted to module top to satisfy
# E402; no circular-import risk because compute_manifest does not import
# compute.
from app.compute_manifest import validate_payload_uri, build_agent_secret_manifest
from app.database import AsyncSessionLocal

# Quota-scope classification (project-global vs regional vs zonal blast radius
# for provisioning errors) lives in app.provision_error_scope — extracted to
# keep this module within its size guardrail. Imported for compute's own use;
# the names also stay reachable as `app.compute.*` (the quota state dicts are
# only ever mutated in place, so these aliases remain live). No load-time
# cycle: provision_error_scope imports compute._zone_lock at call time only.
from app.time_utils import exponential_backoff
from app.provision_error_scope import (
    QUOTA_SCOPE_GLOBAL,
    QUOTA_SCOPE_REGIONAL,
    _GLOBAL_QUOTA_MARKERS,
    _clear_gpu_quota_cooldowns_after_success,
    _clear_quota_cooldowns_after_success,
    _global_quota_state,
    _gpu_global_quota_state,
    _gpu_region_quota_state,
    _record_global_quota_exhaustion,
    _record_gpu_global_quota_exhaustion,
    _record_gpu_region_quota_exhaustion,
    _record_region_quota_exhaustion,
    _region_quota_state,
    _zone_region,
    classify_provision_error_scope,
    get_quota_cooldowns,
)

logger = logging.getLogger(__name__)

# --- Mode Selection ---
LOCAL_MODE = os.getenv("LOCAL_MODE", "false").lower() == "true"

# Configuration for GCP Compute Engine
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "your-gcp-project-id")
GCP_ZONE = os.getenv("GCP_ZONE", "us-central1-f")

# Cross-region fallback: (zone, subnet_name) tuples
# Primary region first, then additional regions with their dedicated subnets
FALLBACK_ZONES: list[tuple[str, str]] = [
    ("us-central1-f", "agent-sandbox-subnet"),
    ("us-central1-c", "agent-sandbox-subnet"),
    ("us-central1-b", "agent-sandbox-subnet"),
    ("us-central1-a", "agent-sandbox-subnet"),
    ("us-east4-b", "agent-sandbox-subnet-us-east4"),
    ("us-east4-a", "agent-sandbox-subnet-us-east4"),
    ("us-east1-c", "agent-sandbox-subnet-us-east1"),
    ("us-east1-b", "agent-sandbox-subnet-us-east1"),
    ("us-west1-b", "agent-sandbox-subnet-us-west1"),
    ("us-west1-a", "agent-sandbox-subnet-us-west1"),
]

# Patterns indicating infrastructure-wide provision failure (not task-specific).
# Used by orchestrator and reaper to avoid burning retry budgets for transient outages.
INFRA_PROVISION_PATTERNS = [
    "all zones in cooldown",
    "all zones exhausted",
    "global provision capacity",
    "zone_resource_pool_exhausted",
    "quota",
    "cpus_all_regions",
    "does not have enough resources",
    "in_use_addresses",
    "503 service unavailable",
]

# Track which zone each VM was created in (for delete/cleanup)
_vm_zones: dict[str, str] = {}

# Name prefixes for all Blueprint-managed GCE instances. The orphan-cleanup
# sweep inventories every prefix here; a new managed-VM family (e.g. the
# compute-job substrate) MUST be added so its VMs are reclaimable.
_MANAGED_VM_PREFIXES = ("agent-vm-", "compute-job-vm-")

# Track per-VM image identity reported via heartbeats: vm_id -> (image_name, build_ts)
_vm_images: dict[str, tuple[str | None, int | None]] = {}

# Track per-VM agent runtime fingerprint (64-hex SHA-256) reported via heartbeat.
_vm_runtime_fingerprints: dict[str, str] = {}

# Track per-VM source git SHA (7-40 hex) baked into the image and reported via
# heartbeat. Distinct from the runtime fingerprint (a content hash of the agent
# source): this is the exact git commit the image was built from, so a baked
# image and a running VM can be mapped back to a specific SHA.
_vm_source_shas: dict[str, str] = {}

# Track when each VM's provisioning slot was actually acquired (ISO timestamp string).
# Written by _provision_gcp after both Redis and local semaphore guards are passed;
# read by the orchestrator to populate _cold_start_stages["provision_slot_acquired_at"].
_vm_provision_slot_times: dict[str, str] = {}
_IMAGE_STALENESS_THRESHOLD_HOURS = int(os.getenv("VM_IMAGE_STALENESS_HOURS", "24"))

# Semaphores to cap concurrent GCP API calls — prevents thread pool starvation
# and stays well under GCP's 20 QPS rate limit.
# Separate limits for slow insert (up to 300s) vs fast ops (list/delete/get, ~10s)
# so long-running provisions don't starve orphan cleanup.
# Local semaphore still used as secondary guard even with Redis (prevents thread starvation).
_MAX_GLOBAL_PROVISIONS = int(os.getenv("MAX_PROVISION_SLOTS", "10"))
_gcp_provision_semaphore = asyncio.Semaphore(
    _MAX_GLOBAL_PROVISIONS
)  # insert/create — long-running (up to 300s)
_gcp_ops_semaphore = asyncio.Semaphore(8)  # list/delete/get — fast ops (~10s)

# Distributed provision semaphore via Redis sorted set (falls back to local-only)
_PROVISION_REDIS_KEY = "blueprint:active-provisions"
_PROVISION_SLOT_TTL = 600  # 10 min — auto-reclaim if process crashes

# Admission must be one Redis operation.  The previous pipeline() performed
# ZCARD and ZADD separately, so concurrent Cloud Run instances could all read
# the same sub-limit count before any of them inserted their lease.  In a cold
# backlog that turned a nominal 10-slot limit into a region-wide insert burst.
_ACQUIRE_PROVISION_SLOT_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local vm_id = ARGV[3]
local expires_at = tonumber(ARGV[4])

redis.call('ZREMRANGEBYSCORE', key, '-inf', now)

-- A retry for an already-admitted VM renews its lease without consuming a
-- second slot or being rejected merely because the set is now full.
if redis.call('ZSCORE', key, vm_id) then
    redis.call('ZADD', key, expires_at, vm_id)
    return 1
end

if redis.call('ZCARD', key) >= limit then
    return 0
end

redis.call('ZADD', key, expires_at, vm_id)
return 1
"""


async def _acquire_provision_slot(vm_id: str) -> bool:
    """Atomically acquire a global provision slot via Redis.

    Redis ``EVAL`` keeps expiry cleanup, capacity inspection, and lease insert
    in one server-side operation.  This is the actual cross-instance guard;
    the process-local semaphore below remains the thread-pool backstop.
    """
    from app.redis_client import get_redis

    r = await get_redis()
    if not r:
        return True  # No Redis — rely on local semaphore only
    try:
        now = time.time()
        admitted = await r.eval(
            _ACQUIRE_PROVISION_SLOT_LUA,
            1,
            _PROVISION_REDIS_KEY,
            now,
            _MAX_GLOBAL_PROVISIONS,
            vm_id,
            now + _PROVISION_SLOT_TTL,
        )
        return bool(int(admitted))
    except Exception as e:
        logger.warning(
            f"[REDIS] Provision slot acquire failed, using local semaphore: {e}"
        )
        return True  # Degrade gracefully


async def _release_provision_slot(vm_id: str):
    """Release a global provision slot."""
    from app.redis_client import get_redis

    r = await get_redis()
    if r:
        try:
            await r.zrem(_PROVISION_REDIS_KEY, vm_id)
        except Exception:
            logger.exception(
                f"[COMPUTE] Failed to release provision slot for {vm_id} (TTL will reclaim)"
            )


# Zone cooldown: skip zones that recently failed (avoid wasting time retrying exhausted zones)
_zone_failures: dict[str, datetime] = {}
ZONE_COOLDOWN_SECONDS = int(
    os.getenv("ZONE_COOLDOWN_BASE_SECONDS", "180")
)  # 3 minutes — base for exponential backoff
ZONE_RECOVERY_WAIT_CAP_SECONDS = int(
    os.getenv("ZONE_RECOVERY_WAIT_CAP_SECONDS", "300")
)  # Max wait when all zones in cooldown


ZONE_HISTORY_WINDOW = int(
    os.getenv("ZONE_HISTORY_WINDOW", "20")
)  # Rolling window size for success rate
_ZONE_NEUTRAL_RATE = 0.5  # Default success rate for zones with no history


def _zone_cooldown_seconds(consecutive_failures: int) -> float:
    """Exponential backoff: 3min → 6min → 12min → 24min → 30min cap, with jitter."""
    return exponential_backoff(consecutive_failures + 1, base=ZONE_COOLDOWN_SECONDS, cap=1800.0)


# Zone health tracking: populated by provisioning attempts
_zone_health: dict[str, dict] = {}
# Structure: {
#   "us-central1-f": {
#     "last_success": datetime, "last_failure": datetime,
#     "consecutive_failures": 0, "last_failure_reason": str,
#     "recent_outcomes": deque[bool],  # True=success, False=failure, maxlen=ZONE_HISTORY_WINDOW
#   }
# }

# GPU-specific zone health — isolated from CPU fleet state so GPU stockouts
# never contaminate CPU zone selection (and vice versa).
_gpu_zone_health: dict[str, dict] = {}
_gpu_zone_failures: dict[str, datetime] = {}


def _zone_success_rate(zone: str, *, gpu: bool = False) -> float:
    """Return the recent success rate for a zone (0.0-1.0).

    When ``gpu=True`` reads from the GPU-isolated health state so GPU zone
    selection is informed by GPU-specific history, not CPU fleet history.
    Returns the neutral rate (0.5) when no history exists so unknown zones
    sort in the middle, neither preferred nor penalised.
    Caller is responsible for holding ``_zone_lock`` when thread safety matters.
    """
    h = _gpu_zone_health if gpu else _zone_health
    outcomes = h.get(zone, {}).get("recent_outcomes")
    if not outcomes:
        return _ZONE_NEUTRAL_RATE
    return sum(outcomes) / len(outcomes)


def _zone_blocked_until(
    zone: str, now: datetime, *, gpu: bool = False
) -> datetime | None:
    """Effective blocking expiry for a zone: the latest of its own cooldown,
    its region's quota cooldown, and the project-global quota cooldown.
    When ``gpu=True`` uses the GPU-isolated health/quota state so GPU failures
    cannot block CPU provisioning (and CPU failures cannot block GPU jobs).
    Returns ``None`` when the zone is not blocked. Caller must hold
    ``_zone_lock`` when thread safety matters."""
    candidates = []
    zh = _gpu_zone_health if gpu else _zone_health
    zone_cd = zh.get(zone, {}).get("cooldown_expires_at")
    if zone_cd and now < zone_cd:
        candidates.append(zone_cd)
    rqs = _gpu_region_quota_state if gpu else _region_quota_state
    region_cd = rqs.get(_zone_region(zone), {}).get("cooldown_expires_at")
    if region_cd and now < region_cd:
        candidates.append(region_cd)
    gqs = _gpu_global_quota_state if gpu else _global_quota_state
    global_cd = gqs.get("cooldown_expires_at")
    if global_cd and now < global_cd:
        candidates.append(global_cd)
    return max(candidates) if candidates else None


# Thread lock for zone state dicts (modified from asyncio.to_thread workers, read from event loop)
_zone_lock = threading.RLock()
MACHINE_TYPE = os.getenv("MACHINE_TYPE", "e2-standard-2")

# Balanced Persistent Disk is the fast default for agent boot volumes, but its
# regional SSD quota is intentionally small on new regions.  When that quota is
# exhausted we can still provision from the same image on pd-standard, whose
# capacity is charged to the much larger DISKS_TOTAL_GB quota.  Keep the match
# narrow so CPU/instance/global quota failures retain their normal cooldown and
# health semantics.
_SSD_BOOT_DISK_QUOTA_MARKERS = (
    "ssd_total_gb",
    "ssd-total-gb",
    "ssd_total_storage",
    "persistent disk ssd",
)


def _is_ssd_boot_disk_quota_exhaustion(error: str) -> bool:
    error_lower = (error or "").lower()
    return "quota" in error_lower and any(
        marker in error_lower for marker in _SSD_BOOT_DISK_QUOTA_MARKERS
    )

# Provisioning model: "STANDARD" (on-demand, full price, no termination) or
# "SPOT" (60-91% cheaper, can be preempted with 30s notice). Default STANDARD
# so local dev / tests don't accidentally request spot capacity. Cloud Run is
# configured to set SPOT explicitly (see backend/cloudbuild.yaml).
VM_PROVISIONING_MODEL = os.getenv("VM_PROVISIONING_MODEL", "STANDARD").upper()
if VM_PROVISIONING_MODEL not in ("STANDARD", "SPOT"):
    logger.warning(
        f"[COMPUTE] Unknown VM_PROVISIONING_MODEL={VM_PROVISIONING_MODEL!r}, "
        f"falling back to STANDARD"
    )
    VM_PROVISIONING_MODEL = "STANDARD"

# Administratively disabled regions — VMs will not be spawned here.
# Accepts `;` or `,` as separator (e.g. "us-east1;us-east4;us-west1").
# `;` is the safe choice when wired through gcloud `--set-env-vars` because
# `,` is gcloud's KEY=VALUE delimiter; using comma there would split the
# region list into separate env vars. Used to mirror terraform: when
# multi-region NAT is dropped from vpc.tf, those regions have no subnet, so
# spawning would fail with cryptic missing-subnet errors. Listing them here
# makes the orchestrator skip with an explicit
# "administratively disabled (no subnet)" log instead.
_DISABLED_REGIONS_RAW = os.getenv("DISABLED_REGIONS", "").replace(";", ",")
_DISABLED_REGIONS = frozenset(
    r.strip() for r in _DISABLED_REGIONS_RAW.split(",") if r.strip()
)


def _zone_in_disabled_region(zone: str) -> bool:
    """True if the zone's region is in DISABLED_REGIONS."""
    return _zone_region(zone) in _DISABLED_REGIONS


def _active_fallback_zones() -> list[tuple[str, str]]:
    """FALLBACK_ZONES with administratively disabled regions filtered out.

    Used by spawn-time iteration. Cleanup / monitoring paths still use the
    full FALLBACK_ZONES list so stranded VMs in newly-disabled regions are
    still found and cleaned (defense-in-depth — though terraform enforces
    no-VMs-without-subnet so this should be empty).
    """
    if not _DISABLED_REGIONS:
        return FALLBACK_ZONES
    return [(z, s) for z, s in FALLBACK_ZONES if not _zone_in_disabled_region(z)]


def _format_orphan_status(status: str) -> str:
    """Augment status string with spot-preemption hint when in SPOT mode.

    A TERMINATED VM in SPOT mode is overwhelmingly likely a preemption rather
    than an orderly shutdown. Tagging the log line distinctly helps operators
    distinguish capacity-driven kills from agent crashes.
    """
    if status == "TERMINATED" and VM_PROVISIONING_MODEL == "SPOT":
        return f"{status} (likely spot preemption)"
    return status


# Module-load-time announce of operational config: visible in Cloud Run startup
# logs so an operator who's debugging "why didn't a VM spawn in us-east1?" sees
# the answer in the first log lines instead of having to grep production code.
if _DISABLED_REGIONS:
    _excluded_zones = [z for z, _ in FALLBACK_ZONES if _zone_in_disabled_region(z)]
    logger.info(
        f"[COMPUTE] DISABLED_REGIONS={sorted(_DISABLED_REGIONS)} — "
        f"{len(_excluded_zones)} of {len(FALLBACK_ZONES)} fallback zones are "
        f"administratively disabled (no subnet provisioned in this region): "
        f"{_excluded_zones}. Active spawn pool: "
        f"{[z for z, _ in _active_fallback_zones()]}"
    )
if VM_PROVISIONING_MODEL == "SPOT":
    logger.info(
        "[COMPUTE] VM_PROVISIONING_MODEL=SPOT — agent VMs will be created with "
        "spot/preemptible pricing. Expect ~5%% mid-task termination rate; "
        "reaper + retry handle re-spawn. Flip to STANDARD via env var to revert.",
    )


# Networking Configuration
VPC_NETWORK = os.getenv("VPC_NETWORK", "agent-sandbox-vpc")
VPC_SUBNET = os.getenv("VPC_SUBNET", "agent-sandbox-subnet")

# The base image for our agent
AGENT_IMAGE_FAMILY = os.getenv(
    "AGENT_IMAGE_FAMILY",
    f"projects/{GCP_PROJECT_ID}/global/images/family/blueprint-agent-v2",
)

# --- Single-GPU provisioning FACTS — ADDITIVE & INERT ---
# These constants/helpers describe how to provision a single-GPU VM. They are
# NOT consumed by _provision_gcp / provision_ephemeral_vm yet; a later subtask
# wires them into the existing cooldown / success-rate / quota machinery. All
# facts come from the gce-gpu-provisioning knowledge document.

# Driver-preinstalled GPU boot image family (Google Deep Learning VM, NVIDIA
# drivers + ML stack + gVNIC preinstalled). Pinned and overridable the same way
# AGENT_IMAGE_FAMILY is. A later subtask may resolve this family to a concrete
# versioned image.
GPU_IMAGE_FAMILY = os.getenv(
    "GPU_IMAGE_FAMILY",
    # Google retires dated CUDA image families: common-cu128-ubuntu-2204-nvidia-570
    # was deleted upstream (provision attempts 404'd, GPU provisioning self-disabled
    # via _ensure_gpu_image_family_valid). Verified-current successor as of 2026-06-08
    # is the cu129 / driver-580 series (see `gcloud compute images list
    # --project deeplearning-platform-release --filter family~common-cu129`). The
    # startup/recheck validation in this module is the live tripwire if this is
    # retired again — no silent fallback list, so a future deprecation stays visible.
    "projects/deeplearning-platform-release/global/images/family/"
    "common-cu129-ubuntu-2204-nvidia-580",
)

# Per-region subnet names derived from the fleet FALLBACK_ZONES ring, so GPU
# zones reuse the existing per-region subnets rather than inventing new ones.
_REGION_SUBNETS: dict[str, str] = {}
for _gpu_zone, _gpu_subnet in FALLBACK_ZONES:
    _REGION_SUBNETS.setdefault(_gpu_zone.rsplit("-", 1)[0], _gpu_subnet)

# GPU-availability zone lists per accelerator (GCP type string), from the
# knowledge doc's GPU-locations table. INDEPENDENT of the fleet FALLBACK_ZONES
# ring: GPU zones differ per accelerator and per region.
_GPU_ZONE_NAMES: dict[str, list[str]] = {
    "nvidia-tesla-t4": [
        "us-central1-a",
        "us-central1-b",
        "us-central1-c",
        "us-central1-f",
        "us-east1-b",
        "us-east1-c",
        "us-east1-d",
        "us-east4-a",
        "us-east4-b",
        "us-east4-c",
        "us-west1-a",
        "us-west1-b",
    ],
    "nvidia-l4": [
        "us-central1-a",
        "us-central1-b",
        "us-central1-c",
        "us-east1-b",
        "us-east1-c",
        "us-east1-d",
        "us-east4-a",
        "us-east4-c",
        "us-west1-a",
        "us-west1-b",
    ],
    "nvidia-tesla-v100": [
        "us-central1-a",
        "us-central1-b",
        "us-central1-c",
        "us-central1-f",
        "us-east1-b",
        "us-east1-c",
        "us-west1-a",
        "us-west1-b",
    ],
}

# Resolved per-accelerator (zone, subnet) rings, reusing the fleet per-region
# subnets. Keyed by GCP accelerator type string.
GPU_FALLBACK_ZONES: dict[str, list[tuple[str, str]]] = {
    _accel: [(_zone, _REGION_SUBNETS[_zone.rsplit("-", 1)[0]]) for _zone in _zones]
    for _accel, _zones in _GPU_ZONE_NAMES.items()
}


def _all_managed_zones() -> list[str]:
    """Deduplicated union of CPU fleet zones and GPU job zones, CPU-first.

    CPU zones from FALLBACK_ZONES appear first so identity/cleanup probes hit
    the common case before searching GPU-only zones. GPU-only zones (those in
    GPU_FALLBACK_ZONES but absent from FALLBACK_ZONES) are appended in the
    order they first appear across accelerators. Used by find_vm_zone,
    stop_vm, _delete_gcp, and _cleanup_gcp so GPU compute-job VMs in
    accelerator-specific zones are discoverable after a process restart and
    are included in orphan cleanup.
    """
    cpu_zones = [z for z, _ in FALLBACK_ZONES]
    seen: set[str] = set(cpu_zones)
    result = list(cpu_zones)
    for zone_list in GPU_FALLBACK_ZONES.values():
        for z, _ in zone_list:
            if z not in seen:
                seen.add(z)
                result.append(z)
    return result


def gpu_zone_candidates(accelerator: str) -> list[tuple[str, str]]:
    """Ordered (zone, subnet) candidates where ``accelerator`` is available.

    Pure helper: returns a fresh list drawn from ``GPU_FALLBACK_ZONES``,
    independent of the fleet ``FALLBACK_ZONES`` ring. Accepts either a GCP
    accelerator type string (e.g. ``"nvidia-tesla-t4"``) or a canonical short
    name (e.g. ``"T4"``). Returns ``[]`` for unknown accelerators.

    The dependent subtask feeds this into the existing cooldown / success-rate /
    quota machinery; it does nothing on its own.
    """
    accel = accelerator
    if accel not in GPU_FALLBACK_ZONES:
        from app.vm_cost_rates import resolve_accelerator_spec

        spec = resolve_accelerator_spec(accelerator)
        if spec is not None:
            accel = spec.gcp_type
    return list(GPU_FALLBACK_ZONES.get(accel, []))


# Scripts baked into the startup script as base64 blobs. Loaded at module
# import time so a missing file fails fast instead of during VM provisioning.
# See backend/app/_agent_scripts/__init__.py for rationale.
_AGENT_SCRIPTS_DIR = Path(__file__).parent / "_agent_scripts"
_MATERIALIZE_MANIFEST_B64 = _base64.b64encode(
    (_AGENT_SCRIPTS_DIR / "materialize_manifest.py").read_bytes()
).decode("ascii")

# The URL where the agent can reach this orchestrator
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8080")

# Anthropic API Key — optional fallback for Claude Code CLI.
# Primary auth is a long-lived OAuth token (sk-ant-oat01-...) from `claude setup-token`.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# The "Naked" Service Account with minimal GCP permissions.
AGENT_SERVICE_ACCOUNT_EMAIL = os.getenv(
    "AGENT_SERVICE_ACCOUNT_EMAIL",
    "vpm-worker@your-gcp-project-id.iam.gserviceaccount.com",
)

# Leaf identity for throwaway compute-image builder VMs (infra/terraform/iam.tf:
# google_service_account.image_builder_sa). Its ONLY grant is Artifact Registry
# writer+reader on the compute-images repo — no orchestrator, GitHub, LLM, or
# runner credentials are reachable from it. The orchestrator SA holds
# roles/iam.serviceAccountUser on it so it can attach it to a builder VM without
# ever handing that VM the orchestrator's own identity. An off-orchestrator,
# repo-controlled (untrusted) ``docker build`` runs under THIS identity, never
# AGENT_SERVICE_ACCOUNT_EMAIL.
IMAGE_BUILDER_SERVICE_ACCOUNT_EMAIL = os.getenv(
    "IMAGE_BUILDER_SERVICE_ACCOUNT_EMAIL",
    "image-builder-sa@your-gcp-project-id.iam.gserviceaccount.com",
)

# Agent source directory for local mode
AGENT_DIR = os.getenv(
    "AGENT_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "agent")
)


def _validate_env_config() -> None:
    """Fail fast if placeholder env vars are still set."""
    if GCP_PROJECT_ID == "your-gcp-project-id":
        raise RuntimeError(
            "GCP_PROJECT_ID is not configured (still set to placeholder)"
        )
    if ORCHESTRATOR_URL == "http://localhost:8080" and not LOCAL_MODE:
        raise RuntimeError(
            "ORCHESTRATOR_URL is not configured (still set to localhost)"
        )
    if "your-gcp-project-id" in AGENT_SERVICE_ACCOUNT_EMAIL and not LOCAL_MODE:
        raise RuntimeError(
            "AGENT_SERVICE_ACCOUNT_EMAIL is not configured (still contains placeholder)"
        )


def _validate_image_family() -> None:
    """Verify the agent VM image family exists in GCP. Prevents all-zone cascading failure."""
    from google.cloud import compute_v1

    try:
        client = compute_v1.ImagesClient()
        # get_from_family raises NotFound if family doesn't exist
        family_name = AGENT_IMAGE_FAMILY.split("/")[-1]
        project = GCP_PROJECT_ID
        if AGENT_IMAGE_FAMILY.startswith("projects/"):
            parts = AGENT_IMAGE_FAMILY.split("/")
            project = parts[1]
        client.get_from_family(project=project, family=family_name)
        logger.info(
            f"[STARTUP] Image family '{family_name}' validated in project '{project}'"
        )
    except Exception as e:
        logger.exception("[STARTUP] Image family validation failed")
        raise RuntimeError(
            f"AGENT_IMAGE_FAMILY '{AGENT_IMAGE_FAMILY}' not found: {e}"
        ) from e


def _validate_gpu_image_family() -> None:
    """Verify the GPU boot image family exists in GCP. Prevents all-zone cascading failure.

    Independent of _validate_image_family — a missing GPU image must not disable
    CPU provisioning.
    """
    from google.cloud import compute_v1

    try:
        client = compute_v1.ImagesClient()
        family_name = GPU_IMAGE_FAMILY.split("/")[-1]
        project = "deeplearning-platform-release"
        if GPU_IMAGE_FAMILY.startswith("projects/"):
            parts = GPU_IMAGE_FAMILY.split("/")
            project = parts[1]
        image = client.get_from_family(project=project, family=family_name)
        logger.info(
            f"[STARTUP] GPU image family '{family_name}' validated in project '{project}'"
            f" — resolved to '{image.name}'"
        )
    except Exception as e:
        logger.exception("[STARTUP] GPU image family validation failed")
        raise RuntimeError(
            f"GPU_IMAGE_FAMILY '{GPU_IMAGE_FAMILY}' not found: {e}"
        ) from e


_image_family_valid: bool | None = None  # None = not yet checked, True/False = result
_image_family_last_check: float = 0.0
_IMAGE_FAMILY_RECHECK_INTERVAL: float = 300.0  # Retry validation every 5 min if failed

# GPU image family validation state — independent of _image_family_valid so a
# bad GPU image cannot disable CPU provisioning.
_gpu_image_family_valid: bool | None = None
_gpu_image_family_last_check: float = 0.0

if not LOCAL_MODE and os.getenv(
    "K_REVISION"
):  # K_REVISION set by Cloud Run — skip in dev/test
    _validate_env_config()
    try:
        _validate_image_family()
        _image_family_valid = True
        _image_family_last_check = time.time()
    except Exception:
        _image_family_valid = False
        _image_family_last_check = time.time()
        logger.exception(
            f"[STARTUP] Image family validation FAILED — will retry in {_IMAGE_FAMILY_RECHECK_INTERVAL}s"
        )
    try:
        _validate_gpu_image_family()
        _gpu_image_family_valid = True
        _gpu_image_family_last_check = time.time()
    except Exception:
        _gpu_image_family_valid = False
        _gpu_image_family_last_check = time.time()
        logger.exception(
            f"[STARTUP] GPU image family validation FAILED — will retry in {_IMAGE_FAMILY_RECHECK_INTERVAL}s"
        )

# Manifest builder + payload URI validator are imported at the top of this
# module so they're re-exported for existing `from app.compute import ...`
# callers (and tests that mock via `app.compute.X`).
_REEXPORTS = (
    validate_payload_uri,
    build_agent_secret_manifest,
)  # keep referenced so linters don't strip the re-exports

ARTIFACT_BUCKET_NAME = os.getenv("ARTIFACT_BUCKET_NAME", "blueprint-agent-artifacts")


# --- Local Mode: Track subprocesses ---
_local_processes: dict[str, subprocess.Popen] = {}


async def prepare_and_store_agent_secret(
    vm_instance_id: str,
    task_id: uuid.UUID,
    oauth_token: str | None = None,
    claude_config_json: str | None = None,
    task_type: str = "",
    provider: str | None = None,
    auth_mode: str | None = None,
    model: str | None = None,
    execution_mode: str | None = None,
    provider_metadata: dict | None = None,
    provider_settings: dict | None = None,
    project_id: str = "",  # audit W3/E10: needed for cross-project authz claim
) -> str | None:
    """Build agent credentials and store them in Secret Manager.

    Returns the Secret Manager version resource name, or None in LOCAL_MODE.
    Callers can run this concurrently with upload_json_payload via asyncio.gather()
    since both are independent I/O operations that feed into the VM startup script.
    """
    if LOCAL_MODE:
        return None
    manifest = build_agent_secret_manifest(
        vm_instance_id=vm_instance_id,
        task_id=task_id,
        oauth_token=oauth_token,
        claude_config_json=claude_config_json,
        task_type=task_type,
        provider=provider,
        auth_mode=auth_mode,
        model=model,
        project_id=project_id,
        execution_mode=execution_mode,
        provider_metadata=provider_metadata,
        provider_settings=provider_settings,
    )
    return await asyncio.to_thread(
        _store_agent_secret,
        vm_instance_id,
        json.dumps(manifest, separators=(",", ":")),
    )


async def provision_agent_vm(
    vm_instance_id: str,
    task_id: uuid.UUID,
    payload_uri: str | None = None,
    oauth_token: str | None = None,
    claude_config_json: str | None = None,
    task_type: str = "",
    secret_version_name: str | None = None,
    provider: str | None = None,
    auth_mode: str | None = None,
    model: str | None = None,
    execution_mode: str | None = None,
    provider_metadata: dict | None = None,
    provider_settings: dict | None = None,
    provisioning_model: str | None = None,
    machine_type: str | None = None,
):
    """
    Provisions an agent. In LOCAL_MODE, spawns a subprocess.
    In production, creates a GCP Compute Engine VM.
    oauth_token: Long-lived OAuth token (sk-ant-oat01-...) for Claude Code CLI.
    claude_config_json: Content of ~/.claude.json for onboarding bypass.
    secret_version_name: Pre-computed Secret Manager version (from prepare_and_store_agent_secret).
        If provided, skips the internal secret build+store step.
    """
    # Every call here is a fresh (cold) provision, so the dispatch site in
    # orchestrator.py writes Task.vm_warm_start=False. Warm reuse never flows
    # through this function: the warm-claim / backlog-reuse path reuses an
    # existing VM and stamps Task.vm_warm_start=True in
    # routers/agent_warm_continuity._finalize_warm_claim, which is also where
    # the vm_reuse_rate signal (context['_timing']['warm_start']) is written.
    if payload_uri:
        validate_payload_uri(payload_uri)
    _ensure_image_family_valid()
    if LOCAL_MODE:
        return await _provision_local(
            vm_instance_id,
            task_id,
            payload_uri,
            oauth_token,
            claude_config_json,
            task_type=task_type,
            provider=provider,
            auth_mode=auth_mode,
            model=model,
            execution_mode=execution_mode,
            provider_metadata=provider_metadata,
            provider_settings=provider_settings,
        )
    return await _provision_gcp(
        vm_instance_id,
        task_id,
        payload_uri,
        oauth_token,
        claude_config_json,
        task_type=task_type,
        secret_version_name=secret_version_name,
        provider=provider,
        auth_mode=auth_mode,
        model=model,
        execution_mode=execution_mode,
        provider_metadata=provider_metadata,
        provider_settings=provider_settings,
        provisioning_model=provisioning_model,
        machine_type=machine_type,
    )


def _ensure_image_family_valid() -> None:
    """Block provisioning when the agent VM image family is invalid.

    Retries validation periodically so a transient startup failure doesn't
    permanently disable provisioning. Raises RuntimeError while invalid. Shared
    by the task-bound (provision_agent_vm) and taskless (provision_pool_vm)
    entrypoints. No-op in LOCAL_MODE.
    """
    global _image_family_valid, _image_family_last_check
    if _image_family_valid is False and not LOCAL_MODE:
        now = time.time()
        if now - _image_family_last_check >= _IMAGE_FAMILY_RECHECK_INTERVAL:
            try:
                _validate_image_family()
                _image_family_valid = True
                _image_family_last_check = now
                logger.info(
                    "[COMPUTE] Image family re-validation succeeded — provisioning re-enabled"
                )
            except Exception as revalidate_err:
                _image_family_last_check = now
                logger.exception("[COMPUTE] Image family re-validation still failing")
                raise RuntimeError(
                    f"VM provisioning disabled: image family '{AGENT_IMAGE_FAMILY}' "
                    f"failed validation. Next retry in {_IMAGE_FAMILY_RECHECK_INTERVAL}s."
                ) from revalidate_err
        else:
            raise RuntimeError(
                f"VM provisioning disabled: image family '{AGENT_IMAGE_FAMILY}' "
                f"failed validation. Next retry in {_IMAGE_FAMILY_RECHECK_INTERVAL - (now - _image_family_last_check):.0f}s."
            )


def _ensure_gpu_image_family_valid() -> None:
    """Block GPU provisioning when the GPU boot image family is invalid.

    Mirrors _ensure_image_family_valid but uses isolated state so a missing GPU
    image does NOT disable CPU provisioning. Raises RuntimeError while invalid.
    No-op in LOCAL_MODE.
    """
    global _gpu_image_family_valid, _gpu_image_family_last_check
    if _gpu_image_family_valid is False and not LOCAL_MODE:
        now = time.time()
        if now - _gpu_image_family_last_check >= _IMAGE_FAMILY_RECHECK_INTERVAL:
            try:
                _validate_gpu_image_family()
                _gpu_image_family_valid = True
                _gpu_image_family_last_check = now
                logger.info(
                    "[COMPUTE] GPU image family re-validation succeeded — GPU provisioning re-enabled"
                )
            except Exception as revalidate_err:
                _gpu_image_family_last_check = now
                logger.exception(
                    "[COMPUTE] GPU image family re-validation still failing"
                )
                raise RuntimeError(
                    f"GPU provisioning disabled: image family '{GPU_IMAGE_FAMILY}' "
                    f"failed validation. Next retry in {_IMAGE_FAMILY_RECHECK_INTERVAL}s."
                ) from revalidate_err
        else:
            raise RuntimeError(
                f"GPU provisioning disabled: image family '{GPU_IMAGE_FAMILY}' "
                f"failed validation. Next retry in {_IMAGE_FAMILY_RECHECK_INTERVAL - (now - _gpu_image_family_last_check):.0f}s."
            )


async def provision_pool_vm(
    session,
    vm_instance_id: str,
    project_id: uuid.UUID,
    *,
    account_id: str | None = None,
    oauth_token: str | None = None,
    claude_config_json: str | None = None,
    provider: str | None = None,
    auth_mode: str | None = None,
    model: str | None = None,
    agent_image_fingerprint: str | None = None,
    execution_mode: str | None = None,
    provider_metadata: dict | None = None,
    provider_settings: dict | None = None,
    provisioning_model: str | None = None,
    machine_type: str | None = None,
) -> str | None:
    """Boot a TASKLESS, credentialed warm-pool VM and register it in PoolVm.

    The VM is pinned to ``account_id`` / ``provider`` / ``auth_mode`` / ``model``
    (baked into its boot secret manifest) but carries NO task. It boots poll-first
    (BLUEPRINT_POOL_MODE) and authenticates with a VM-scoped token. On success a
    :class:`app.models.PoolVm` row is added to ``session`` (the caller commits) and the
    ``vm_instance_id`` is returned; returns ``None`` (no row registered) when the GCP
    provision fails without raising, so the caller must NOT commit on a None return.

    INFRASTRUCTURE ONLY — gated by ``warm_pool_enabled`` (default False); raises
    RuntimeError when the pool is disabled so the path is inert in production.
    The maintainer (provisions toward target), the dispatch lease hook
    (``lease_idle_pool_vm``), and the boot-heartbeat readiness promotion
    (``warm_pool.promote_pool_vm_to_idle``, called from the /vm-heartbeat
    handler) are all live; this
    function's job is only to boot a credentialed taskless member and register
    it PROVISIONING. Mirrors provision_agent_vm's image-family validation and
    provider/account plumbing.
    """
    from app.warm_pool import is_warm_pool_enabled, register_pool_vm
    from app.models import PoolVmState

    if not is_warm_pool_enabled():
        raise RuntimeError(
            "warm_pool_enabled is False — pool VM provisioning is disabled"
        )

    _ensure_image_family_valid()

    project_id_str = str(project_id)

    if LOCAL_MODE:
        # No GCP VM in local mode; register the member so the registry is
        # exercisable end-to-end without a cloud provisioner. Nothing dispatches
        # to it yet (infra only).
        await register_pool_vm(
            session,
            vm_instance_id=vm_instance_id,
            project_id=project_id,
            account_id=account_id,
            provider=provider,
            auth_mode=auth_mode,
            model=model,
            agent_image_fingerprint=agent_image_fingerprint,
            state=PoolVmState.PROVISIONING,
        )
        logger.info(
            f"[WARM-POOL] LOCAL_MODE pool VM registered (no boot): {vm_instance_id}"
        )
        return vm_instance_id

    from app.compute_manifest import build_taskless_agent_secret_manifest
    from app.compute_startup_script import build_pool_agent_startup_script

    # Build the taskless manifest (VM-scoped token, no TASK_ID, pinned creds)
    # and store it in Secret Manager — same store path as the task-bound flow.
    manifest = build_taskless_agent_secret_manifest(
        vm_instance_id=vm_instance_id,
        project_id=project_id_str,
        oauth_token=oauth_token,
        claude_config_json=claude_config_json,
        provider=provider,
        auth_mode=auth_mode,
        model=model,
        execution_mode=execution_mode,
        provider_metadata=provider_metadata,
        provider_settings=provider_settings,
    )
    secret_version_name = await asyncio.to_thread(
        _store_agent_secret,
        vm_instance_id,
        json.dumps(manifest, separators=(",", ":")),
    )

    startup_script = build_pool_agent_startup_script(
        vm_instance_id=vm_instance_id,
        secret_version_name=secret_version_name,
        orchestrator_url=ORCHESTRATOR_URL,
        materialize_manifest_b64=_MATERIALIZE_MANIFEST_B64,
    )

    success = await _provision_gcp(
        vm_instance_id,
        None,  # taskless
        secret_version_name=secret_version_name,
        provider=provider,
        auth_mode=auth_mode,
        model=model,
        execution_mode=execution_mode,
        provider_metadata=provider_metadata,
        provider_settings=provider_settings,
        provisioning_model=provisioning_model,
        machine_type=machine_type,
        startup_script_override=startup_script,
    )
    if not success:
        # _provision_gcp returns False WITHOUT raising on its normal failure paths
        # (provision-capacity reached, 420s boot timeout/CancelledError, caught
        # exception) and deletes the staged agent secret on each. Committing a
        # PROVISIONING PoolVm row here would register a PHANTOM member whose secret
        # is already gone and which can never reach IDLE (only the zombie reaper
        # would eventually clean it up). Mirror provision_agent_vm's success gate
        # and bail; the maintainer simply retries next tick.
        logger.warning(
            "[WARM-POOL] _provision_gcp did not boot pool VM %s (project %s) — "
            "skipping registry insert (secret already cleaned up by provisioner)",
            vm_instance_id,
            project_id,
        )
        return None

    # Register the freshly-booted member in PROVISIONING. It becomes leasable
    # (IDLE) when its agent fires the boot-time /vm-heartbeat readiness signal,
    # promoted by warm_pool.promote_pool_vm_to_idle from the /vm-heartbeat
    # handler; the lease path (lease_idle_pool_vm) only ever selects IDLE members.
    await register_pool_vm(
        session,
        vm_instance_id=vm_instance_id,
        project_id=project_id,
        account_id=account_id,
        provider=provider,
        auth_mode=auth_mode,
        model=model,
        agent_image_fingerprint=agent_image_fingerprint,
        state=PoolVmState.PROVISIONING,
    )
    return vm_instance_id


_last_provision_error: _OrderedDict = _OrderedDict()
_PROVISION_ERROR_MAX = 500
_NO_POOL_VM_UPDATE = object()


def get_provision_error(vm_instance_id: str) -> str:
    """Get and clear the last provisioning error for a VM."""
    return _last_provision_error.pop(vm_instance_id, "")


def get_vm_zone(vm_instance_id: str) -> str:
    """Get the zone a VM was provisioned in."""
    return _vm_zones.get(vm_instance_id, "unknown")


def find_vm_zone(vm_name: str) -> str | None:
    """Restart-safe zone discovery: locate a VM by name across fallback zones.

    The in-memory ``_vm_zones`` map is empty after a leader restart, so a service
    that must inspect a VM provisioned by a previous process (the compute-job
    monitor) cannot rely on ``get_vm_zone`` alone. This probes each fallback zone
    for the instance, repopulates ``_vm_zones`` on a hit, and returns ``None`` if
    the VM exists nowhere. Synchronous GCP RPCs — wrap in ``asyncio.to_thread``
    when called from async code.
    """
    if not vm_name:
        return None
    cached = _vm_zones.get(vm_name)
    if cached and cached != "unknown":
        return cached
    for zone in _all_managed_zones():
        status = get_vm_status(vm_name, zone)
        # get_vm_status returns a synthetic "DELETED" for a 404 (not in this
        # zone); any other non-None status means the VM lives in this zone.
        if status and status != "DELETED":
            with _zone_lock:
                _vm_zones[vm_name] = zone
            return zone
    return None


def record_vm_image(
    vm_instance_id: str, image_name: str | None, build_ts: int | None
) -> None:
    """Record the boot-disk image identity reported by a VM via heartbeat."""
    if not vm_instance_id:
        return
    _vm_images[vm_instance_id] = (image_name, build_ts)
    logger.debug(
        f"[COMPUTE] VM {vm_instance_id} image={image_name} build_ts={build_ts}"
    )


def get_vm_image(vm_instance_id: str) -> tuple[str | None, int | None]:
    """Return (image_name, build_ts) for a VM, or (None, None) if unknown."""
    return _vm_images.get(vm_instance_id, (None, None))


def record_vm_runtime_fingerprint(vm_instance_id: str, fingerprint: str) -> None:
    """Record the agent-runtime SHA-256 fingerprint reported by a VM via heartbeat."""
    if not vm_instance_id:
        return
    _vm_runtime_fingerprints[vm_instance_id] = fingerprint
    logger.debug(
        f"[COMPUTE] VM {vm_instance_id} agent_runtime_fingerprint={fingerprint[:12]}…"
    )


def get_vm_runtime_fingerprint(vm_instance_id: str) -> str | None:
    """Return the agent-runtime fingerprint for a VM, or None if not yet reported."""
    return _vm_runtime_fingerprints.get(vm_instance_id)


def record_vm_source_sha(vm_instance_id: str, source_sha: str) -> None:
    """Record the source git SHA (baked into the image) reported by a VM via heartbeat."""
    if not vm_instance_id:
        return
    _vm_source_shas[vm_instance_id] = source_sha
    logger.debug(f"[COMPUTE] VM {vm_instance_id} agent_source_sha={source_sha[:12]}…")


def get_vm_source_sha(vm_instance_id: str) -> str | None:
    """Return the source git SHA reported by a VM, or None if not yet reported."""
    return _vm_source_shas.get(vm_instance_id)


async def fleet_serving_source_sha(
    session,
    promoted_sha: str | None,
    *,
    allow_descendant: bool = False,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    project_id: str | None = None,
) -> bool:
    """True iff the running agent fleet is observed serving ``promoted_sha``.

    Default fleet-serving definition for the agent VM image release artifact: at
    least one currently-active (LIVE: PROVISIONING / IDLE / LEASED) warm-pool VM
    reports ``agent_source_sha == promoted_sha`` — i.e. the freshly baked image
    is live and running on the fleet.  DB-backed so the read survives an
    orchestrator restart (the durable ``PoolVm.agent_source_sha`` is populated by
    heartbeats via :func:`persist_pool_vm_image_state`); the in-memory heartbeat
    map is consulted as a warm fallback before a row is persisted (and for
    task-bound VMs that have no PoolVm registry row).

    A read-only observation: returns ``False`` (not-yet-serving) rather than
    raising on any error so it can never abort the serving-confirmation sweep.
    """
    if not promoted_sha:
        return False
    observed_shas: set[str] = set()
    try:
        from sqlalchemy import select

        from app.models import PoolVm
        from app.warm_pool import LIVE_POOL_STATES

        rows = (
            await session.execute(
                select(PoolVm.agent_source_sha).where(
                    PoolVm.agent_source_sha.is_not(None),
                    PoolVm.state.in_(LIVE_POOL_STATES),
                )
            )
        ).all()
        observed_shas.update(str(row[0]) for row in rows if row[0])
        if promoted_sha in observed_shas:
            return True
    except Exception as e:
        logger.debug(f"[COMPUTE] fleet_serving_source_sha DB read failed: {e}")
    # In-memory heartbeat fallback (warm before a PoolVm row is persisted).
    observed_shas.update(sha for sha in _vm_source_shas.values() if sha)
    if promoted_sha in observed_shas:
        return True
    if not (allow_descendant and token and owner and repo):
        return False
    try:
        from app import github_refs

        for source_sha in observed_shas:
            ancestry = await github_refs.is_ancestor_of(
                token,
                owner,
                repo,
                promoted_sha,
                source_sha,
                project_id=project_id,
            )
            if ancestry is True:
                return True
    except Exception as e:
        logger.debug(f"[COMPUTE] fleet source ancestry check failed: {e}")
    return False


async def agent_fleet_source_sha_snapshot(session, project_id=None) -> dict:
    """Return a read-only source-SHA distribution for live agent VMs.

    Durable ``PoolVm`` rows are the restart-safe source for warm-pool members.
    The in-memory heartbeat caches add short-lived task VMs that have no pool
    registry row. Missing ``agent_source_sha`` is preserved as an explicit
    unknown bucket so older/pre-marker images do not look healthy by omission.
    """
    observations: dict[str, dict] = {}
    try:
        from sqlalchemy import select

        from app.models import PoolVm, Task, TaskStatus
        from app.warm_pool import LIVE_POOL_STATES

        stmt = select(PoolVm).where(PoolVm.state.in_(LIVE_POOL_STATES))
        if project_id is not None:
            stmt = stmt.where(PoolVm.project_id == project_id)
        members = (await session.execute(stmt)).scalars().all()
        for member in members:
            vm_id = member.vm_instance_id
            if not vm_id:
                continue
            observations[vm_id] = {
                "vm_id": vm_id,
                "source_sha": member.agent_source_sha,
                "image_name": member.agent_image_name,
                "build_ts": member.agent_image_build_ts,
                "last_heartbeat_at": member.last_heartbeat_at.isoformat()
                if member.last_heartbeat_at
                else None,
                "kind": "warm_pool",
            }

        cached_vm_ids = set(_vm_source_shas) | set(_vm_images)
        if cached_vm_ids:
            task_stmt = select(Task.vm_instance_id).where(
                Task.vm_instance_id.in_(cached_vm_ids),
                Task.status.in_((TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS)),
            )
            if project_id is not None:
                task_stmt = task_stmt.where(Task.project_id == project_id)
            task_vm_ids = (await session.execute(task_stmt)).scalars().all()
            for vm_id in task_vm_ids:
                if not vm_id or vm_id in observations:
                    continue
                image_name, build_ts = _vm_images.get(vm_id, (None, None))
                observations[vm_id] = {
                    "vm_id": vm_id,
                    "source_sha": _vm_source_shas.get(vm_id),
                    "image_name": image_name,
                    "build_ts": build_ts,
                    "last_heartbeat_at": None,
                    "kind": "task_vm",
                }
    except Exception as e:
        logger.debug(f"[COMPUTE] agent_fleet_source_sha_snapshot failed: {e}")

    buckets: dict[str | None, dict] = {}
    for obs in observations.values():
        sha = obs.get("source_sha")
        bucket = buckets.setdefault(
            sha,
            {
                "source_sha": sha,
                "count": 0,
                "warm_pool_count": 0,
                "task_vm_count": 0,
                "image_names": [],
                "latest_build_ts": None,
                "last_heartbeat_at": None,
            },
        )
        bucket["count"] += 1
        if obs.get("kind") == "warm_pool":
            bucket["warm_pool_count"] += 1
        else:
            bucket["task_vm_count"] += 1
        image_name = obs.get("image_name")
        if image_name and image_name not in bucket["image_names"]:
            bucket["image_names"].append(image_name)
        build_ts = obs.get("build_ts")
        if build_ts is not None and (
            bucket["latest_build_ts"] is None or build_ts > bucket["latest_build_ts"]
        ):
            bucket["latest_build_ts"] = build_ts
        heartbeat_at = obs.get("last_heartbeat_at")
        if heartbeat_at and (
            bucket["last_heartbeat_at"] is None
            or heartbeat_at > bucket["last_heartbeat_at"]
        ):
            bucket["last_heartbeat_at"] = heartbeat_at

    buckets_out = sorted(
        buckets.values(),
        key=lambda b: (b["source_sha"] is None, -b["count"], b["source_sha"] or ""),
    )
    return {
        "total_observed": len(observations),
        "unknown_count": sum(
            1 for obs in observations.values() if not obs.get("source_sha")
        ),
        "mixed": len([b for b in buckets_out if b["source_sha"]]) > 1,
        "buckets": buckets_out,
    }


async def expected_serving_source_sha(session, project_id=None) -> str | None:
    """Best-effort: the source git SHA the live agent fleet is currently serving.

    Returns the dominant (most-observed) ``agent_source_sha`` across live agent
    VMs from :func:`agent_fleet_source_sha_snapshot`, or ``None`` when no live VM
    has reported a source SHA yet. The snapshot already sorts buckets so the
    first non-null bucket is the most-observed served SHA. Read-only and never
    raises — callers that compare a grader VM's recorded SHA against this for a
    staleness tag must treat ``None`` as "expected SHA unknown" and skip the tag.
    """
    try:
        snap = await agent_fleet_source_sha_snapshot(session, project_id=project_id)
        for bucket in snap.get("buckets") or []:
            sha = bucket.get("source_sha")
            if sha:
                return str(sha)
        return None
    except Exception as e:
        logger.debug(f"[COMPUTE] expected_serving_source_sha failed: {e}")
        return None


async def persist_pool_vm_image_state(
    session,
    vm_instance_id: str,
    *,
    image_name: str | None | object = _NO_POOL_VM_UPDATE,
    build_ts: int | None | object = _NO_POOL_VM_UPDATE,
    runtime_fingerprint: str | None | object = _NO_POOL_VM_UPDATE,
    source_sha: str | None | object = _NO_POOL_VM_UPDATE,
    heartbeat_at: datetime | None = None,
) -> bool:
    """Persist heartbeat-derived image identity onto a PoolVm row when present.

    The in-memory heartbeat caches are enough for short-lived alerting, but they
    vanish on orchestrator restart. This helper mirrors the observed image/source
    identity onto the durable ``PoolVm`` registry row for warm-pool members so
    later release-artifact checks can reason about the fleet across restarts.

    Returns ``True`` when a matching PoolVm row was updated, ``False`` when the
    VM is not a warm-pool member (task-bound chain/backlog VMs have no PoolVm row).
    """
    if not vm_instance_id:
        return False
    from app.warm_pool import get_pool_vm
    from app.time_utils import _utcnow

    member = await get_pool_vm(session, vm_instance_id)
    if member is None:
        return False
    if image_name is not _NO_POOL_VM_UPDATE:
        member.agent_image_name = image_name
    if build_ts is not _NO_POOL_VM_UPDATE:
        member.agent_image_build_ts = build_ts
    if runtime_fingerprint is not _NO_POOL_VM_UPDATE:
        member.agent_runtime_fingerprint = runtime_fingerprint
    if source_sha is not _NO_POOL_VM_UPDATE:
        member.agent_source_sha = source_sha
    member.last_heartbeat_at = heartbeat_at or _utcnow()
    session.add(member)
    return True


def get_image_source_git_sha(image_name: str, project: str | None = None) -> str | None:
    """Read the ``source-git-sha`` label off a GCP image, or None when absent.

    Best-effort, read-only provenance reader: maps a baked agent VM image back to
    the exact git commit it was built from (the label set by the Packer bake in
    infra/packer/agent.pkr.hcl). Returns None on LOCAL_MODE, any GCP error, a
    missing label, or the baked ``unknown`` fallback (a build with no commit in
    the environment). No VM cooperation required — the label lives on the image.
    """
    if LOCAL_MODE or not image_name:
        return None

    from google.cloud import compute_v1

    proj = project or GCP_PROJECT_ID
    try:
        client = compute_v1.ImagesClient()
        image = client.get(project=proj, image=image_name)
        sha = (image.labels or {}).get("source-git-sha")
    except Exception as e:  # provenance read must never raise to callers
        logger.debug(f"[COMPUTE] get_image_source_git_sha({image_name}) failed: {e}")
        return None
    if not sha or sha == "unknown":
        return None
    return sha


def _image_creation_epoch(image) -> int | None:
    """Parse a GCP image ``creation_timestamp`` (ISO-8601) to epoch seconds, or None."""
    ts = getattr(image, "creation_timestamp", None)
    if not ts:
        return None
    try:
        from datetime import datetime as _dt

        return int(_dt.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def get_current_agent_image(
    family: str | None = None, project: str | None = None
) -> dict | None:
    """Resolve the image the agent VM image family currently points at.

    Read-only, best-effort: maps the live ``blueprint-agent-v2`` family target
    back to its name, its baked ``source-git-sha`` provenance label, and a build
    timestamp (epoch seconds, parsed from the image ``creation_timestamp``). This
    is the 'current valid image' the fleet should converge to — a VM booted from
    an older image is pre-promotion and should be recycled once this newer image
    exists.

    Returns ``{"image_name", "source_git_sha", "build_ts"}`` or ``None`` in
    LOCAL_MODE / on any GCP error / when the family has no target. The baked
    ``unknown`` SHA fallback is normalised to ``None`` (mirrors
    :func:`get_image_source_git_sha`).
    """
    if LOCAL_MODE:
        return None

    from google.cloud import compute_v1

    fam_input = family or AGENT_IMAGE_FAMILY
    fam_name = fam_input.split("/")[-1]
    proj = project or GCP_PROJECT_ID
    if fam_input.startswith("projects/"):
        proj = fam_input.split("/")[1]
    try:
        client = compute_v1.ImagesClient()
        image = client.get_from_family(project=proj, family=fam_name)
    except Exception as e:  # provenance read must never raise to callers
        logger.debug(f"[COMPUTE] get_current_agent_image({fam_name}) failed: {e}")
        return None
    source_sha = (image.labels or {}).get("source-git-sha")
    if source_sha == "unknown":
        source_sha = None
    return {
        "image_name": image.name,
        "source_git_sha": source_sha or None,
        "build_ts": _image_creation_epoch(image),
    }


def get_outdated_image_vms(current_build_ts: int | None = None) -> list[dict]:
    """Return VMs whose reported image predates the current image-family target.

    Distinct from :func:`get_stale_image_vms` (a fixed temporal age threshold):
    this compares each VM's heartbeat-reported ``build_ts`` against the build
    timestamp of the image the family CURRENTLY points at. A VM is flagged as
    soon as a newer valid image lands — even if it is younger than the staleness
    threshold — so warm-pool / maintenance flows recycle pre-promotion boots and
    the fleet converges on the promoted image instead of lingering on old boots.

    ``current_build_ts`` may be supplied to avoid a redundant family lookup;
    otherwise it is resolved via :func:`get_current_agent_image`. Returns ``[]``
    in LOCAL_MODE / when the current image (or its build_ts) cannot be resolved
    — there is no newer valid image to converge to, so nothing is 'outdated'.
    """
    if current_build_ts is None:
        current = get_current_agent_image()
        current_build_ts = current.get("build_ts") if current else None
    if current_build_ts is None:
        return []
    outdated: list[dict] = []
    for vm_id, (image_name, build_ts) in list(_vm_images.items()):
        if build_ts is not None and build_ts < current_build_ts:
            outdated.append(
                {
                    "vm_id": vm_id,
                    "image_name": image_name,
                    "build_ts": build_ts,
                    "current_build_ts": current_build_ts,
                    "agent_runtime_fingerprint": _vm_runtime_fingerprints.get(vm_id),
                }
            )
    return outdated


def get_provision_slot_acquired_at(vm_instance_id: str) -> str | None:
    """Return the ISO timestamp when this VM's provisioning slot was acquired, or None."""
    return _vm_provision_slot_times.get(vm_instance_id)


def _clear_vm_heartbeat_state(vm_instance_id: str) -> None:
    """Drop heartbeat-derived metadata once a VM is no longer running."""
    _vm_images.pop(vm_instance_id, None)
    _vm_runtime_fingerprints.pop(vm_instance_id, None)
    _vm_source_shas.pop(vm_instance_id, None)
    _vm_provision_slot_times.pop(vm_instance_id, None)


def get_stale_image_vms(threshold_hours: int | None = None) -> list[dict]:
    """Return VMs whose image build_ts is older than threshold_hours.

    Used by alerting to surface VMs running pre-Chromium-bake (or other
    stale) images before they cause task-level failures.
    """
    cutoff_hours = (
        threshold_hours
        if threshold_hours is not None
        else _IMAGE_STALENESS_THRESHOLD_HOURS
    )
    cutoff_ts = int(time.time()) - cutoff_hours * 3600
    stale = []
    for vm_id, (image_name, build_ts) in list(_vm_images.items()):
        if build_ts is not None and build_ts < cutoff_ts:
            stale.append(
                {
                    "vm_id": vm_id,
                    "image_name": image_name,
                    "build_ts": build_ts,
                    "age_hours": round((time.time() - build_ts) / 3600, 1),
                    "agent_runtime_fingerprint": _vm_runtime_fingerprints.get(vm_id),
                }
            )
    return stale


async def delete_agent_vm(vm_instance_id: str):
    """
    Terminates an agent. In LOCAL_MODE, kills the subprocess.
    In production, deletes the GCP VM.
    """
    if LOCAL_MODE:
        return await _delete_local(vm_instance_id)
    return await _delete_gcp(vm_instance_id)


async def cleanup_orphaned_vms(active_vm_ids: list[str]):
    """
    Cleans up orphaned agents.
    """
    if LOCAL_MODE:
        return await _cleanup_local(active_vm_ids)
    return await _cleanup_gcp(active_vm_ids)


async def cleanup_old_images(
    keep: int,
    family: str | None = None,
    project: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Delete oldest custom images in `family`, retaining the `keep` most recent.

    GCP enforces a per-project image quota; without pruning, each Packer build leaves the
    previous images behind and eventually provisioning fails with quota errors. This function
    lists images in the family sorted by creationTimestamp and issues delete for everything
    past the `keep` boundary. Running VMs already booted from those images are unaffected —
    Compute Engine only references the source image at instance-create time.

    Returns a dict with: total (in family), deleted (count), kept, failed, dry_run.
    """
    if keep < 1:
        raise ValueError(
            "keep must be >= 1 (must retain at least one image for VM provisioning)"
        )
    if LOCAL_MODE:
        return {
            "total": 0,
            "deleted": 0,
            "kept": 0,
            "failed": [],
            "dry_run": dry_run,
            "skipped": "local_mode",
        }

    from google.cloud import compute_v1

    fam_input = family or AGENT_IMAGE_FAMILY
    fam_name = fam_input.split("/")[-1]
    proj = project or GCP_PROJECT_ID
    if fam_input.startswith("projects/"):
        parts = fam_input.split("/")
        proj = parts[1]

    def _list_and_sort():
        client = compute_v1.ImagesClient()
        # Filter by family; API does not accept server-side sort, so we sort locally.
        req = compute_v1.ListImagesRequest(project=proj, filter=f"family={fam_name}")
        images = list(client.list(request=req))
        # Newest first (ISO-8601 timestamps sort lexically).
        images.sort(key=lambda im: im.creation_timestamp or "", reverse=True)
        return images

    images = await asyncio.to_thread(_list_and_sort)
    total = len(images)
    if total <= keep:
        return {
            "total": total,
            "deleted": 0,
            "kept": total,
            "failed": [],
            "dry_run": dry_run,
        }

    to_delete = images[keep:]
    failed: list[dict] = []

    if dry_run:
        return {
            "total": total,
            "deleted": 0,
            "kept": keep,
            "failed": [],
            "dry_run": True,
            "would_delete": [im.name for im in to_delete],
        }

    def _delete_one(name: str) -> str | None:
        try:
            client = compute_v1.ImagesClient()
            client.delete(project=proj, image=name)
            return None
        except Exception as e:
            return str(e)[:300]

    deleted = 0
    for im in to_delete:
        err = await asyncio.to_thread(_delete_one, im.name)
        if err:
            failed.append({"name": im.name, "error": err})
            logger.warning(f"[IMAGE CLEANUP] Failed to delete {im.name}: {err}")
        else:
            deleted += 1
            logger.info(f"[IMAGE CLEANUP] Deleted {im.name}")

    return {
        "total": total,
        "deleted": deleted,
        "kept": keep,
        "failed": failed,
        "dry_run": False,
    }


# ─── Local Mode Implementation ──────────────────────────────────────────────


async def _provision_local(
    vm_instance_id: str,
    task_id: uuid.UUID,
    payload_uri: str | None = None,
    oauth_token: str | None = None,
    claude_config_json: str | None = None,
    task_type: str = "",
    provider: str | None = None,
    auth_mode: str | None = None,
    model: str | None = None,
    execution_mode: str | None = None,
    provider_metadata: dict | None = None,
    provider_settings: dict | None = None,
):
    logger.info(
        f"[LOCAL] Spawning agent subprocess: {vm_instance_id} (Task: {task_id})"
    )
    manifest = build_agent_secret_manifest(
        vm_instance_id=vm_instance_id,
        task_id=task_id,
        oauth_token=oauth_token,
        claude_config_json=claude_config_json,
        task_type=task_type,
        provider=provider,
        auth_mode=auth_mode,
        model=model,
        execution_mode=execution_mode,
        provider_metadata=provider_metadata,
        provider_settings=provider_settings,
    )
    env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    env.update(_materialize_local_manifest(vm_instance_id, manifest))
    env["PAYLOAD_URI"] = payload_uri or ""

    agent_dir = os.path.abspath(AGENT_DIR)
    if not os.path.isdir(agent_dir):
        logger.error(f"[LOCAL] Agent directory not found: {agent_dir}")
        return False

    try:
        proc = await asyncio.to_thread(
            lambda: subprocess.Popen(
                ["npm", "start"],
                cwd=agent_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )
        _local_processes[vm_instance_id] = proc
        logger.info(f"[LOCAL] Agent subprocess started (PID: {proc.pid})")

        # Start a background task to log output and clean up
        async def _monitor():
            try:
                await asyncio.to_thread(proc.wait)
            finally:
                _local_processes.pop(vm_instance_id, None)
                _clear_vm_heartbeat_state(vm_instance_id)
                logger.info(
                    f"[LOCAL] Agent subprocess exited (PID: {proc.pid}, code: {proc.returncode})"
                )

        asyncio.create_task(_monitor())  # noqa: RUF006 — fire-and-forget monitor; lives for the subprocess lifetime
        return True
    except Exception:
        logger.exception("[LOCAL] Failed to spawn agent")
        return False


async def _delete_local(vm_instance_id: str):
    proc = _local_processes.pop(vm_instance_id, None)
    if proc and proc.poll() is None:
        logger.info(
            f"[LOCAL] Killing agent subprocess: {vm_instance_id} (PID: {proc.pid})"
        )
        try:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.to_thread(proc.wait, timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            logger.exception("[LOCAL] Failed to kill subprocess")
            return False
    try:
        local_root = _local_manifest_root(vm_instance_id)
        if local_root.exists():
            import shutil

            await asyncio.to_thread(shutil.rmtree, local_root, True)
    except Exception:
        logger.warning(f"[LOCAL] Failed to clean manifest files for {vm_instance_id}")
    _clear_vm_heartbeat_state(vm_instance_id)
    return True


async def _cleanup_local(active_vm_ids: list[str]):
    orphaned = [vid for vid in _local_processes if vid not in active_vm_ids]
    for vid in orphaned:
        logger.warning(f"[LOCAL] Cleaning up orphaned agent: {vid}")
        await _delete_local(vid)
    return len(orphaned)


# ─── GCP Mode Implementation ────────────────────────────────────────────────


def _store_agent_secret(vm_instance_id: str, secret_payload: str) -> str:
    """
    Store agent credentials in GCP Secret Manager.
    Returns the full resource name of the created secret version.
    Agent SA reads via project-level secretAccessor (Terraform-managed).
    """
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    secret_id = f"agent-creds-{vm_instance_id}"
    parent = f"projects/{GCP_PROJECT_ID}"

    # Create the secret (or reuse if it already exists from a previous attempt)
    secret_name = f"{parent}/secrets/{secret_id}"
    try:
        secret = client.create_secret(
            request={
                "parent": parent,
                "secret_id": secret_id,
                "secret": {
                    "replication": {"automatic": {}},
                    "labels": {
                        "managed-by": "blueprint",
                        "vm-instance": vm_instance_id,
                    },
                },
            }
        )
    except Exception as e:
        if "AlreadyExists" in str(type(e).__name__) or "409" in str(e):
            logger.info(f"Secret {secret_id} already exists — reusing")
            secret = type("Secret", (), {"name": secret_name})()
        else:
            raise

    # Add the secret version with the payload (overwrites any stale version)
    version = client.add_secret_version(
        request={
            "parent": secret.name,
            "payload": {"data": secret_payload.encode("utf-8")},
        }
    )

    # Agent SA holds secretAccessor scoped to agent-creds-* secrets only (via
    # IAM condition on google_project_iam_member.agent_secret_accessor in
    # infra/terraform/iam.tf). No runtime get/setIamPolicy calls needed —
    # orchestrator does not hold secretmanager.admin.

    # E15: verify the version is actually accessible before handing it off
    # to provisioning. Without this, transient Secret Manager replication
    # lag or IAM-binding races can yield a version.name that points at a
    # not-yet-readable version, which only surfaces 60+ seconds later when
    # the agent VM tries to fetch it and times out into AGENT_DIED_EARLY.
    try:
        verification = client.access_secret_version(request={"name": version.name})
        if verification.payload.data != secret_payload.encode("utf-8"):
            logger.error(
                "[COMPUTE] Secret %s post-create payload mismatch (size=%d vs expected=%d)",
                version.name,
                len(verification.payload.data),
                len(secret_payload),
            )
            raise RuntimeError(
                f"Secret {version.name} verification failed: payload mismatch"
            )
    except Exception as verify_err:
        logger.exception(
            "[COMPUTE] Secret %s failed post-create verification: %s",
            version.name,
            verify_err,
        )
        raise

    logger.info(f"Stored agent credentials in Secret Manager: {secret_id}")
    return version.name


def _local_manifest_root(vm_instance_id: str) -> Path:
    return Path(tempfile.gettempdir()) / "blueprint-agent-state" / vm_instance_id


def _materialize_local_manifest(vm_instance_id: str, manifest: dict) -> dict[str, str]:
    """Write manifest files locally and return env vars for the subprocess."""
    root = _local_manifest_root(vm_instance_id)
    root.mkdir(parents=True, exist_ok=True)

    path_map: dict[str, str] = {}
    env = {
        str(key): str(value)
        for key, value in (manifest.get("env") or {}).items()
        if value is not None
    }
    for item in manifest.get("files", []) or []:
        original_path = str(item["path"])
        # Pre-check: reject path traversal sequences before any processing.
        if ".." in original_path.split("/"):
            raise ValueError(
                f"Path traversal detected in manifest file path: {original_path!r} "
                f"(contains '..' segment)"
            )
        local_path = root / original_path.lstrip("/").replace("/", os.sep)
        # Resolve symlinks and verify the final path stays within root.
        resolved = local_path.resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            raise ValueError(
                f"Manifest file path {original_path!r} resolves outside "
                f"the manifest root: {resolved} not under {root}"
            )
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(str(item.get("content", "")), encoding="utf-8")
        try:
            os.chmod(local_path, int(str(item.get("mode", "0600")), 8))
        except OSError:
            logger.warning(f"[LOCAL] Failed to chmod local manifest file: {local_path}")
        path_map[original_path] = str(local_path)

    for original, localized in path_map.items():
        original_parent = os.path.dirname(original.rstrip("/"))
        localized_parent = os.path.dirname(localized)
        for key, value in list(env.items()):
            if value == original:
                env[key] = localized
            elif original_parent and value == original_parent:
                env[key] = localized_parent

    return env


def _delete_agent_secret(vm_instance_id: str) -> None:
    """Delete the agent credentials secret from GCP Secret Manager (best-effort)."""
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    secret_name = f"projects/{GCP_PROJECT_ID}/secrets/agent-creds-{vm_instance_id}"
    try:
        client.delete_secret(request={"name": secret_name})
        logger.info(f"Deleted agent credentials secret: agent-creds-{vm_instance_id}")
    except Exception as e:
        logger.warning(f"Failed to delete agent secret {secret_name}: {e}")


def _list_managed_secrets() -> list[tuple[str, str, datetime]]:
    """List all blueprint-managed secrets with their vm_instance_id and create_time."""
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{GCP_PROJECT_ID}"
    results: list[tuple[str, str, datetime]] = []
    for secret in client.list_secrets(
        request={"parent": parent, "filter": "labels.managed-by=blueprint"}
    ):
        vm_id = secret.labels.get("vm-instance", "")
        if vm_id:
            results.append((secret.name, vm_id, secret.create_time))
    return results


async def reconcile_orphaned_secrets(active_vm_ids: set[str]) -> dict:
    """Delete secrets whose VM is no longer active. Skips secrets < 30 min old."""
    if LOCAL_MODE:
        return {"listed": 0, "deleted": 0, "skipped_young": 0, "failed": 0}

    async with _gcp_ops_semaphore:
        secrets = await asyncio.to_thread(_list_managed_secrets)

    now = datetime.now(timezone.utc)
    stats: dict[str, int] = {
        "listed": len(secrets),
        "deleted": 0,
        "skipped_young": 0,
        "skipped_active": 0,
        "failed": 0,
    }
    for _secret_name, vm_id, create_time in secrets:
        if vm_id in active_vm_ids:
            stats["skipped_active"] += 1
            continue
        # Ensure create_time is timezone-aware for comparison
        ct = (
            create_time
            if create_time.tzinfo
            else create_time.replace(tzinfo=timezone.utc)
        )
        age_seconds = (now - ct).total_seconds()
        if age_seconds < 1800:  # < 30 min — could be mid-provision
            stats["skipped_young"] += 1
            continue
        try:
            async with _gcp_ops_semaphore:
                await asyncio.to_thread(_delete_agent_secret, vm_id)
            stats["deleted"] += 1
        except Exception:
            stats["failed"] += 1
    return stats


async def _provision_gcp(
    vm_instance_id: str,
    task_id: uuid.UUID | None,
    payload_uri: str | None = None,
    oauth_token: str | None = None,
    claude_config_json: str | None = None,
    task_type: str = "",
    secret_version_name: str | None = None,
    provider: str | None = None,
    auth_mode: str | None = None,
    model: str | None = None,
    execution_mode: str | None = None,
    provider_metadata: dict | None = None,
    provider_settings: dict | None = None,
    provisioning_model: str | None = None,
    startup_script_override: str | None = None,
    machine_type: str | None = None,
    accelerator: str | None = None,
    accelerator_count: int = 1,
    service_account_email: str | None = None,
):
    """Provision a GCP VM. When ``startup_script_override`` is provided it is used
    verbatim as the instance startup-script (the taskless warm-pool path passes
    a pre-built pool script + a pre-stored secret), and ``task_id`` may be None.

    When ``accelerator`` names a supported single-GPU shape, the VM is provisioned
    as a GPU instance: GPU-availability zone candidates (not the fleet ring), a
    guest accelerator for N1+T4/V100 (built-in for G2+L4), the driver-preinstalled
    GPU image family, and ``on_host_maintenance=TERMINATE`` (GPUs cannot
    live-migrate). CPU jobs and agent/pool VMs are unaffected — every GPU branch is
    gated on ``accel_spec`` being non-None.
    """
    from google.cloud import compute_v1

    # Resolve the accelerator request to a supported single-GPU shape. None ⇒ a
    # plain CPU VM, so all GPU-specific branches below are inert for CPU jobs.
    accel_spec = None
    if accelerator is not None:
        from app.vm_cost_rates import resolve_accelerator_spec

        accel_spec = resolve_accelerator_spec(accelerator)
        if accel_spec is None:
            # A GPU was requested but is not a supported single-GPU shape. Fail
            # loudly rather than silently falling back to a CPU VM.
            raise RuntimeError(
                f"unsupported accelerator {accelerator!r} for GPU provisioning"
            )
    is_gpu = accel_spec is not None

    logger.info(
        f"Starting provisioning for VM: {vm_instance_id} "
        f"(Task: {task_id if task_id is not None else 'pool'}"
        f"{f', GPU: {accel_spec.name}' if is_gpu else ''})"
    )
    # GPU jobs boot the GPU machine shape (n1-standard-1 / g2-standard-4); CPU
    # jobs keep the caller's machine type or the fleet default, unchanged.
    effective_machine_type = machine_type or (
        accel_spec.machine_type if is_gpu else MACHINE_TYPE
    )

    if secret_version_name is None and task_id is not None:
        # Secret was not pre-computed by the caller — build and store it now.
        # (Callers that run upload_json_payload concurrently pass secret_version_name
        # from prepare_and_store_agent_secret to avoid this sequential fallback.)
        secret_version_name = await prepare_and_store_agent_secret(
            vm_instance_id,
            task_id,
            oauth_token,
            claude_config_json,
            task_type,
            provider=provider,
            auth_mode=auth_mode,
            model=model,
            execution_mode=execution_mode,
            provider_metadata=provider_metadata,
            provider_settings=provider_settings,
        )

    # payload_uri is validated upstream and safe to embed in the startup script
    import base64

    payload_uri_b64 = (
        base64.b64encode(payload_uri.encode()).decode() if payload_uri else ""
    )

    if startup_script_override is not None:
        # Taskless warm-pool path: the caller (provision_pool_vm) supplies a
        # pre-built pool startup script that beacons via VM-scoped /vm-heartbeat.
        startup_script = startup_script_override
    else:
        from app.compute_startup_script import (
            build_agent_startup_script,
            resolve_vm_ttl_seconds,
        )

        startup_script = build_agent_startup_script(
            task_id=str(task_id),
            secret_version_name=secret_version_name,
            payload_uri_b64=payload_uri_b64,
            orchestrator_url=ORCHESTRATOR_URL,
            materialize_manifest_b64=_MATERIALIZE_MANIFEST_B64,
            # Dedicated cold VM for a known task → apply the per-task-type
            # self-destruct ceiling (compute_experiment gets 24h; default 4h).
            ttl_seconds=resolve_vm_ttl_seconds(task_type),
        )

    instance_config = compute_v1.Instance()
    instance_config.name = vm_instance_id

    initialize_params = compute_v1.AttachedDiskInitializeParams()
    # GPU jobs boot the driver-preinstalled Deep Learning VM GPU image family
    # (NVIDIA drivers + ML stack baked in); CPU jobs keep the agent image.
    initialize_params.source_image = GPU_IMAGE_FAMILY if is_gpu else AGENT_IMAGE_FAMILY
    # GPU jobs keep the larger disk: the Deep Learning VM image (NVIDIA/CUDA + ML
    # stack) plus experiment datasets need the room. The agent image is a ~20GB
    # baked image (infra/packer/agent.pkr.hcl) whose real per-task peak is ~15-18GB
    # (docker deps image, git mirror, npm cache, Playwright Chromium all pre-baked),
    # so 30GB is ample and nearly doubles how many agent VMs fit under the regional
    # SSD_TOTAL_GB quota (50GB→30GB = 30→50 VMs at a 1500GB limit). Floor is the
    # 20GB source-image size; 30GB leaves ~12-15GB headroom over peak.
    initialize_params.disk_size_gb = 50 if is_gpu else 30

    disk = compute_v1.AttachedDisk()
    disk.boot = True
    disk.auto_delete = True
    disk.initialize_params = initialize_params
    instance_config.disks = [disk]

    network_interface = compute_v1.NetworkInterface()
    network_interface.network = (
        f"projects/{GCP_PROJECT_ID}/global/networks/{VPC_NETWORK}"
    )
    # Default subnet — will be overridden per-zone in the fallback loop
    default_region = "-".join(GCP_ZONE.split("-")[:-1])
    network_interface.subnetwork = (
        f"projects/{GCP_PROJECT_ID}/regions/{default_region}/subnetworks/{VPC_SUBNET}"
    )
    instance_config.network_interfaces = [network_interface]

    metadata = compute_v1.Metadata()
    metadata_item = compute_v1.Items()
    metadata_item.key = "startup-script"
    metadata_item.value = startup_script
    serial_logging_item = compute_v1.Items()
    serial_logging_item.key = "serial-port-logging-enable"
    # Disable Cloud Logging ingestion of serial console output. Diagnostics use
    # the Compute API getSerialPortOutput (in-memory ring buffer, ~compute.py:2063),
    # which is independent of this flag, so disabling it saves Cloud Logging
    # ingestion costs with no loss of observability.
    serial_logging_item.value = "FALSE"
    metadata.items = [metadata_item, serial_logging_item]
    instance_config.metadata = metadata

    service_account = compute_v1.ServiceAccount()
    # A caller may attach a narrower leaf identity (e.g. the image-builder SA for
    # an untrusted off-orchestrator docker build) instead of the orchestrator's
    # own agent SA. Absent ⇒ the agent SA, unchanged for every existing caller.
    service_account.email = service_account_email or AGENT_SERVICE_ACCOUNT_EMAIL
    # Security note: cloud-platform is the ONLY OAuth2 scope that covers Secret Manager.
    # There is no narrower secretmanager-specific scope. The actual security boundary is
    # the agent SA's IAM bindings (see infra/terraform/iam.tf):
    #   - secretAccessor scoped to agent-creds-* secrets only (IAM condition in iam.tf)
    #   - objectViewer on the artifact bucket only
    #   - logWriter for Stackdriver logs
    service_account.scopes = [
        "https://www.googleapis.com/auth/devstorage.read_only",
        "https://www.googleapis.com/auth/logging.write",
        "https://www.googleapis.com/auth/cloud-platform",
    ]
    instance_config.service_accounts = [service_account]

    # Apply spot/preemptible scheduling when configured. SPOT VMs are 60-91%
    # cheaper but can be terminated by GCP with 30s notice when capacity is
    # needed. Existing reaper + retry handles re-spawn on termination. Default
    # is STANDARD; cloudbuild.yaml sets VM_PROVISIONING_MODEL=SPOT in prod.
    # provisioning_model overrides the process-wide default (e.g. to force
    # STANDARD after repeated preemptions on a specific task).
    _effective_provisioning_model = (
        provisioning_model or VM_PROVISIONING_MODEL
    ).upper()
    scheduling = None
    if _effective_provisioning_model == "SPOT":
        scheduling = compute_v1.Scheduling()
        scheduling.provisioning_model = "SPOT"
        # DELETE on preemption: Spot VMs we provision are single-task and can't
        # be restarted (the agent's startup-script work isn't idempotent against
        # mid-run interruption). Letting GCP DELETE them on preemption avoids
        # accumulating TERMINATED zombies that cleanup has to chase.
        scheduling.instance_termination_action = "DELETE"
    if is_gpu:
        # GPUs cannot live-migrate — the instance MUST terminate on host
        # maintenance or the insert is rejected. automatic_restart is allowed
        # (and useful) only for STANDARD VMs; SPOT GPU VMs are best-effort
        # capacity, so leave restart at its preemption default.
        scheduling = scheduling or compute_v1.Scheduling()
        scheduling.on_host_maintenance = "TERMINATE"
        if _effective_provisioning_model != "SPOT":
            scheduling.automatic_restart = True
    if scheduling is not None:
        instance_config.scheduling = scheduling

    _cancel_event = threading.Event()

    def _insert_instance():
        instance_client = compute_v1.InstancesClient()
        last_error = None
        skipped_cooldown = []

        def _insert_with_boot_disk_fallback(zone: str):
            """Insert once with pd-balanced, falling back only on SSD quota.

            A regional SSD_TOTAL_GB rejection says nothing about VM inventory or
            zone health.  Retrying the exact same instance with pd-standard keeps
            runnable work flowing without marking the region unhealthy while a
            quota increase is pending.  Other failures bubble unchanged into the
            existing scope classifier.
            """

            operation = instance_client.insert(
                project=GCP_PROJECT_ID,
                zone=zone,
                instance_resource=instance_config,
            )
            try:
                operation.result(timeout=240)
                return operation
            except Exception as exc:
                if is_gpu or not _is_ssd_boot_disk_quota_exhaustion(str(exc)):
                    raise

                logger.warning(
                    "[PROVISION] %s hit SSD boot-disk quota in %s; retrying "
                    "with pd-standard (DISKS_TOTAL_GB quota)",
                    vm_instance_id,
                    zone,
                )
                disk.initialize_params.disk_type = (
                    f"zones/{zone}/diskTypes/pd-standard"
                )
                instance_config.disks = [disk]
                fallback_operation = instance_client.insert(
                    project=GCP_PROJECT_ID,
                    zone=zone,
                    instance_resource=instance_config,
                )
                fallback_operation.result(timeout=240)
                return fallback_operation

        now_pre = datetime.now(timezone.utc)
        # Project-global quota cooldown: a CPUS/GPUS_ALL_REGIONS-style exhaustion
        # blocks EVERY zone identically, so cycling zones would only burn time
        # and re-trip the quota. Fail fast; the message contains "quota" so the
        # orchestrator/reaper INFRA_PROVISION_PATTERNS classify it infra-wide
        # (requeue without burning task retry budget). GPU jobs use the isolated
        # GPU quota state so a GPUS_ALL_REGIONS event never blocks CPU provisions.
        _active_global_quota = (
            _gpu_global_quota_state if is_gpu else _global_quota_state
        )
        global_quota_cd = _active_global_quota.get("cooldown_expires_at")
        if global_quota_cd and now_pre < global_quota_cd:
            remaining = (global_quota_cd - now_pre).total_seconds()
            raise Exception(
                f"Global quota cooldown active — project-wide quota exhausted "
                f"(e.g. {'GPUS' if is_gpu else 'CPUS'}_ALL_REGIONS); retry in {remaining:.0f}s"
            )
        # Local aliases for zone health/failure state — GPU jobs use isolated
        # dicts so failures never cross-contaminate CPU and GPU provisioning lanes.
        _active_zone_health = _gpu_zone_health if is_gpu else _zone_health
        _active_zone_failures = _gpu_zone_failures if is_gpu else _zone_failures
        _record_global_quota = (
            _record_gpu_global_quota_exhaustion
            if is_gpu
            else _record_global_quota_exhaustion
        )
        _record_region_quota = (
            _record_gpu_region_quota_exhaustion
            if is_gpu
            else _record_region_quota_exhaustion
        )
        _clear_quota_success = (
            _clear_gpu_quota_cooldowns_after_success
            if is_gpu
            else _clear_quota_cooldowns_after_success
        )
        # GPU jobs draw from the per-accelerator GPU-availability (zone, subnet)
        # ring (GPU zones differ per accelerator and from the fleet ring); CPU
        # jobs use the fleet ring. Either way admin-disabled regions (no subnet)
        # are filtered out, and the SAME cooldown / success-rate / quota machinery
        # below applies. An empty GPU ring (all zones disabled) fails loudly via
        # the "All zones exhausted" path rather than falling back to a CPU VM.
        if is_gpu:
            active_zones = [
                (z, s)
                for z, s in gpu_zone_candidates(accel_spec.gcp_type)
                if not _zone_in_disabled_region(z)
            ]
        else:
            active_zones = _active_fallback_zones()
        healthy_zones = [
            (z, s)
            for z, s in active_zones
            if _zone_blocked_until(z, now_pre, gpu=is_gpu) is None
        ]
        cooled_zones = [(z, s) for z, s in active_zones if (z, s) not in healthy_zones]
        # Sort healthy zones by recent success rate (descending) so well-performing
        # zones are preferred. Zones with no history get neutral rate 0.5.
        # Use stable sort with a small random tiebreak to distribute load across
        # equal-rate zones (e.g. all-new zones with 0.5 default).
        healthy_zones.sort(
            key=lambda zs: (_zone_success_rate(zs[0], gpu=is_gpu), random.random()),
            reverse=True,
        )
        zones_to_try = healthy_zones + cooled_zones

        for zone, subnet_name in zones_to_try:
            if _cancel_event.is_set():
                logger.info(
                    f"[PROVISION] {vm_instance_id} cancelled — skipping remaining zones"
                )
                return None
            now = datetime.now(
                timezone.utc
            )  # Refresh each iteration — zone attempts can take up to 300s
            # Skip blocked zones — per-zone exponential cooldown, region quota
            # cooldown, or the project-global quota cooldown (a regional/global
            # quota event earlier in THIS loop must skip the remaining zones it
            # covers instead of burning a 240s insert on each).
            if _zone_blocked_until(zone, now, gpu=is_gpu) is not None:
                skipped_cooldown.append(zone)
                continue

            try:
                # Update zone-dependent config for this attempt
                instance_config.machine_type = (
                    f"zones/{zone}/machineTypes/{effective_machine_type}"
                )
                # GPU jobs attach a zone-qualified guest accelerator for N1+T4/V100;
                # G2+L4 carries the GPU in the machine type (bundled), so no guest
                # accelerator entry. CPU jobs never touch guest_accelerators.
                if is_gpu and not accel_spec.bundled:
                    gpu_accel = compute_v1.AcceleratorConfig()
                    gpu_accel.accelerator_type = (
                        f"projects/{GCP_PROJECT_ID}/zones/{zone}"
                        f"/acceleratorTypes/{accel_spec.gcp_type}"
                    )
                    gpu_accel.accelerator_count = accelerator_count
                    instance_config.guest_accelerators = [gpu_accel]
                zone_region = "-".join(zone.split("-")[:-1])
                network_interface.subnetwork = f"projects/{GCP_PROJECT_ID}/regions/{zone_region}/subnetworks/{subnet_name}"
                # pd-balanced (SSD-backed) boots faster than the pd-standard
                # (HDD) default; disk_type is zone-qualified so it must track the
                # zone being attempted. Mutate disk.initialize_params (not the
                # pre-loop local) because the proto copied the params on assignment.
                disk.initialize_params.disk_type = f"zones/{zone}/diskTypes/pd-balanced"
                # Re-assign to ensure protobuf picks up the change
                instance_config.network_interfaces = [network_interface]
                instance_config.disks = [disk]

                # Delete any existing VM with this name first (stale from retries)
                try:
                    instance_client.get(
                        project=GCP_PROJECT_ID, zone=zone, instance=vm_instance_id
                    )
                    logger.info(
                        f"Deleting stale VM {vm_instance_id} in {zone} before re-provisioning"
                    )
                    del_op = instance_client.delete(
                        project=GCP_PROJECT_ID, zone=zone, instance=vm_instance_id
                    )
                    del_op.result(timeout=300)
                except Exception as e:
                    if "was not found" not in str(e) and "404" not in str(e):
                        logger.warning(
                            f"Failed to clean stale VM {vm_instance_id} in {zone}: {e}"
                        )

                # Inner wait is bounded below the outer 420s wait_for so we
                # don't routinely burn the full budget on a single zone.  An
                # SSD boot-disk quota rejection retries this same insert on
                # pd-standard before quota scope/cooldown classification.
                operation = _insert_with_boot_disk_fallback(zone)

                with _zone_lock:
                    _vm_zones[vm_instance_id] = zone
                    health = _active_zone_health.setdefault(zone, {})
                    health["last_success"] = datetime.now(timezone.utc)
                    prev_failures = health.get("consecutive_failures", 0)
                    health["consecutive_failures"] = 0
                    health.pop("cooldown_expires_at", None)
                    outcomes = health.setdefault(
                        "recent_outcomes", _deque(maxlen=ZONE_HISTORY_WINDOW)
                    )
                    outcomes.append(True)
                    _clear_quota_success(zone)
                    was_in_cooldown = zone in _active_zone_failures
                    cooldown_since = _active_zone_failures.pop(zone, None)

                if was_in_cooldown and prev_failures > 0:
                    cooldown_duration = (
                        datetime.now(timezone.utc) - cooldown_since
                    ).total_seconds()
                    logger.warning(
                        f"Zone {zone} recovered from cooldown after {cooldown_duration:.0f}s "
                        f"(had {prev_failures} consecutive failure(s))"
                    )

                logger.info(f"Successfully provisioned VM {vm_instance_id} in {zone}")
                return operation.name
            except Exception as e:
                last_error = e
                err_str = str(e)
                # 409 "already exists": a prior in-flight insert for this same
                # vm_id landed a VM in this zone despite our outer timeout.
                # Adopt it if it's healthy, otherwise delete and retry the
                # insert in the same zone once.
                if "409" in err_str and "already exists" in err_str.lower():
                    try:
                        existing = instance_client.get(
                            project=GCP_PROJECT_ID, zone=zone, instance=vm_instance_id
                        )
                        existing_status = getattr(existing, "status", "") or ""
                        if existing_status in ("RUNNING", "STAGING", "PROVISIONING"):
                            logger.warning(
                                f"[PROVISION] {vm_instance_id} already exists in {zone} "
                                f"(status={existing_status}) — adopting rather than recreating"
                            )
                            with _zone_lock:
                                _vm_zones[vm_instance_id] = zone
                                health = _active_zone_health.setdefault(zone, {})
                                health["last_success"] = datetime.now(timezone.utc)
                                health["consecutive_failures"] = 0
                                health.pop("cooldown_expires_at", None)
                                outcomes = health.setdefault(
                                    "recent_outcomes",
                                    _deque(maxlen=ZONE_HISTORY_WINDOW),
                                )
                                outcomes.append(True)
                                _active_zone_failures.pop(zone, None)
                            return f"adopted-{vm_instance_id}-in-{zone}"
                        logger.warning(
                            f"[PROVISION] {vm_instance_id} exists in {zone} but status={existing_status} — "
                            f"deleting and retrying insert"
                        )
                        del_op = instance_client.delete(
                            project=GCP_PROJECT_ID, zone=zone, instance=vm_instance_id
                        )
                        del_op.result(timeout=180)
                    except Exception as adopt_exc:
                        logger.warning(
                            f"[PROVISION] {vm_instance_id} 409 recovery in {zone} failed: {adopt_exc}"
                        )
                    # Fall through to the retryable-error branch so we try the next zone
                    # if adoption didn't succeed.
                    continue
                # Retryable zone/infra errors — try next zone
                err_low = err_str.lower()
                retryable = (
                    "does not have enough resources" in err_low
                    or "zone_resource_pool_exhausted" in err_low
                    or "quota" in err_low
                    or any(m in err_low for m in _GLOBAL_QUOTA_MARKERS)
                    or "subnetwork"
                    in err_low  # Subnet not found (infra not provisioned)
                    or "was not found" in err_str
                )
                if retryable:
                    # Classify the error's blast radius BEFORE charging the zone:
                    # quota errors are project-global or regional conditions —
                    # marking the attempted zone (let alone every zone in the
                    # cycle) paints healthy zones failed and stacks exponential
                    # cooldowns that outlive the quota event.
                    scope = classify_provision_error_scope(err_str)
                    if scope == QUOTA_SCOPE_GLOBAL:
                        # _record_{gpu_}global_quota_exhaustion carries deduped
                        # ERROR-level operator alert for CPU; this is the per-attempt log.
                        cooldown_secs = _record_global_quota(err_str)
                        logger.warning(
                            f"[PROVISION] Project-global quota exhausted while provisioning "
                            f"{vm_instance_id} in {zone} — project-level cooldown "
                            f"{cooldown_secs}s, NO zone marked: {err_str[:200]}"
                        )
                        # Every remaining zone would fail identically — stop cycling.
                        raise
                    if scope == QUOTA_SCOPE_REGIONAL:
                        region = _zone_region(zone)
                        cooldown_secs = _record_region_quota(region, err_str)
                        logger.warning(
                            f"Region {region} quota exhausted for {vm_instance_id} — "
                            f"region cooldown {cooldown_secs}s, no zone marked; "
                            f"trying zones in other regions..."
                        )
                        # The per-iteration _zone_blocked_until check skips the
                        # remaining zones in this region.
                        continue

                    # Genuinely zonal — classify failure reason for operators
                    if "zone_resource_pool_exhausted" in err_low:
                        reason = "RESOURCE_POOL_EXHAUSTED"
                    elif "subnetwork" in err_low or "was not found" in err_str:
                        reason = "SUBNET_NOT_FOUND"
                    else:
                        reason = "RESOURCE_UNAVAILABLE"

                    with _zone_lock:
                        now_fail = datetime.now(timezone.utc)
                        entering_cooldown = zone not in _active_zone_failures
                        _active_zone_failures[zone] = now_fail
                        health = _active_zone_health.setdefault(zone, {})
                        old_cf = health.get("consecutive_failures", 0)
                        last_fail_time = health.get("last_failure")
                        # Debounce parallel failures: with N concurrent slots, a single zone
                        # hiccup can trigger N simultaneous failures, jumping consecutive_failures
                        # from 0→N and causing exponential cooldown escalation. If the last
                        # failure was within the base cooldown window, treat this as part of the
                        # same failure event — update the timestamp but don't increment the counter.
                        within_debounce = (
                            last_fail_time is not None
                            and (now_fail - last_fail_time).total_seconds()
                            < ZONE_COOLDOWN_SECONDS
                        )
                        # Parallel failure of same event — no escalation
                        new_failure_count = old_cf if within_debounce else old_cf + 1
                        health["last_failure"] = now_fail
                        health["consecutive_failures"] = new_failure_count
                        health["last_failure_reason"] = reason
                        cooldown_secs = _zone_cooldown_seconds(new_failure_count)
                        health["cooldown_expires_at"] = now_fail + timedelta(
                            seconds=cooldown_secs
                        )
                        outcomes = health.setdefault(
                            "recent_outcomes", _deque(maxlen=ZONE_HISTORY_WINDOW)
                        )
                        outcomes.append(False)

                    if entering_cooldown:
                        logger.warning(
                            f"Zone {zone} entering cooldown: {new_failure_count} consecutive failure(s), "
                            f"reason={reason}, cooldown={ZONE_COOLDOWN_SECONDS}s"
                        )
                    logger.warning(
                        f"Zone {zone} unavailable ({reason}) for {vm_instance_id}, trying next zone..."
                    )
                    continue
                # Non-retryable error — don't retry
                raise

        if skipped_cooldown:
            logger.warning(
                f"Skipped {len(skipped_cooldown)} zones in cooldown: {skipped_cooldown}"
            )
        if not last_error and len(skipped_cooldown) == len(active_zones):
            # All ACTIVE zones in cooldown — find the one expiring soonest and wait (up to 2 min).
            # Disabled zones are excluded; they have no subnet, so even waiting wouldn't help.
            now_check = datetime.now(timezone.utc)
            soonest_zone = None
            soonest_wait = float("inf")
            for z, sn in active_zones:
                # Effective block = zone cooldown OR region/global quota cooldown
                expires = _zone_blocked_until(z, now_check, gpu=is_gpu)
                if expires:
                    remaining = (expires - now_check).total_seconds()
                    if remaining < soonest_wait:
                        soonest_wait = remaining
                        soonest_zone = (z, sn)
            if soonest_zone and soonest_wait <= ZONE_RECOVERY_WAIT_CAP_SECONDS:
                wait_zone, wait_subnet = soonest_zone
                logger.warning(
                    f"All zones in cooldown — waiting {soonest_wait:.0f}s for {wait_zone} to exit cooldown"
                )
                time.sleep(max(0, soonest_wait) + 1)
                # Retry the soonest zone
                try:
                    instance_config.machine_type = (
                        f"zones/{wait_zone}/machineTypes/{effective_machine_type}"
                    )
                    # Re-point the zone-qualified guest accelerator at the wait zone
                    # (the URL is zonal). G2+L4 stays bundled in the machine type.
                    if is_gpu and not accel_spec.bundled:
                        gpu_accel = compute_v1.AcceleratorConfig()
                        gpu_accel.accelerator_type = (
                            f"projects/{GCP_PROJECT_ID}/zones/{wait_zone}"
                            f"/acceleratorTypes/{accel_spec.gcp_type}"
                        )
                        gpu_accel.accelerator_count = accelerator_count
                        instance_config.guest_accelerators = [gpu_accel]
                    zone_region = "-".join(wait_zone.split("-")[:-1])
                    network_interface.subnetwork = f"projects/{GCP_PROJECT_ID}/regions/{zone_region}/subnetworks/{wait_subnet}"
                    disk.initialize_params.disk_type = (
                        f"zones/{wait_zone}/diskTypes/pd-balanced"
                    )
                    # Re-assign to ensure protobuf picks up the change
                    instance_config.network_interfaces = [network_interface]
                    instance_config.disks = [disk]
                    operation = _insert_with_boot_disk_fallback(wait_zone)
                    with _zone_lock:
                        _vm_zones[vm_instance_id] = wait_zone
                        health = _active_zone_health.setdefault(wait_zone, {})
                        health["last_success"] = datetime.now(timezone.utc)
                        health["consecutive_failures"] = 0
                        health.pop("cooldown_expires_at", None)
                        outcomes = health.setdefault(
                            "recent_outcomes", _deque(maxlen=ZONE_HISTORY_WINDOW)
                        )
                        outcomes.append(True)
                        _clear_quota_success(wait_zone)
                        _active_zone_failures.pop(wait_zone, None)
                    logger.info(
                        f"Successfully provisioned VM {vm_instance_id} in {wait_zone} (after cooldown wait)"
                    )
                    return operation.name
                except Exception:
                    logger.exception(
                        f"Retry after cooldown wait failed for {wait_zone}"
                    )
                    raise
            else:
                logger.critical(
                    f"ALL {len(active_zones)} active zones in cooldown, soonest expires in {soonest_wait:.0f}s "
                    f"(>{ZONE_RECOVERY_WAIT_CAP_SECONDS}s cap) — failing this provision attempt"
                )
                raise Exception(f"All zones in cooldown — soonest: {soonest_wait:.0f}s")
        raise last_error or Exception("All zones exhausted")

    # Distributed provision slot (Redis sorted set with TTL fallback)
    if not await _acquire_provision_slot(vm_instance_id):
        _last_provision_error[vm_instance_id] = "Global provision capacity reached"
        if len(_last_provision_error) > _PROVISION_ERROR_MAX:
            _last_provision_error.popitem(last=False)
        await asyncio.to_thread(_delete_agent_secret, vm_instance_id)
        return False

    try:
        async with _gcp_provision_semaphore:  # Local guard still useful for thread pool
            # Both Redis slot and local semaphore are now held — stamp queue-wait boundary
            # so the orchestrator can split dispatch→slot (throttle time) from slot→boot.
            _vm_provision_slot_times[vm_instance_id] = datetime.now(
                timezone.utc
            ).isoformat()
            # 420s outer = 240s inner insert + 180s headroom for stale-delete,
            # zone iteration, and secret setup. Previously 360s, which routinely
            # cancelled near-complete GCP operations (surfaced as
            # OPERATION_CANCELED_BY_USER 409 from the in-flight thread).
            operation_name = await asyncio.wait_for(
                asyncio.to_thread(_insert_instance), timeout=420
            )
        logger.info(f"GCP Operation completed: {operation_name}")
        return True
    except (asyncio.TimeoutError, asyncio.CancelledError) as e:
        _cancel_event.set()  # Signal thread to stop trying more zones
        # Our own 420s budget fired — GCP insert/zone-iteration didn't return in
        # time. Orchestrator requeues on attempt 1, so this is handled recovery,
        # not a novel error. Log at WARN (same pattern as OPERATION_CANCELED_BY_USER
        # below) so it stops fouling "UnknownError" dashboards.
        kind = type(e).__name__
        detail = str(e) or f"{kind} (no message)"
        logger.warning(
            f"[PROVISION] {vm_instance_id} aborted after 420s budget ({kind}) — "
            f"orchestrator will requeue: {detail[:200]}"
        )
        _last_provision_error[vm_instance_id] = f"{kind}: {detail}"[:300]
        if len(_last_provision_error) > _PROVISION_ERROR_MAX:
            _last_provision_error.popitem(last=False)
        await asyncio.to_thread(_delete_agent_secret, vm_instance_id)
        return False
    except Exception as e:
        _cancel_event.set()
        err_str = str(e)
        # OPERATION_CANCELED_BY_USER is a side-effect of our own outer timeout
        # firing while GCP's insert long-running-op was still executing — not
        # a novel error. Log at WARN instead of exception so it stops fouling
        # "UnknownError" dashboards.
        if "OPERATION_CANCELED_BY_USER" in err_str:
            logger.warning(
                f"[PROVISION] {vm_instance_id} GCP operation cancelled mid-flight "
                f"(our outer timeout fired while insert was still running): {err_str[:200]}"
            )
        elif "quota" in err_str.lower():
            # Quota exhaustion (global or regional) is a known, handled
            # condition: the quota cooldown is armed, the orchestrator/reaper
            # classify it infra-wide and requeue without burning retry budget.
            # WARN instead of a traceback so it stops fouling error dashboards.
            logger.warning(
                f"[PROVISION] {vm_instance_id} blocked by quota exhaustion: {err_str[:200]}"
            )
        else:
            logger.exception(f"Failed to provision VM {vm_instance_id}")
        _last_provision_error[vm_instance_id] = err_str[:300]
        if len(_last_provision_error) > _PROVISION_ERROR_MAX:
            _last_provision_error.popitem(last=False)
        await asyncio.to_thread(_delete_agent_secret, vm_instance_id)
        return False
    finally:
        await _release_provision_slot(vm_instance_id)


async def provision_ephemeral_vm(
    vm_instance_id: str,
    *,
    startup_script: str,
    machine_type: str | None = None,
    provisioning_model: str | None = None,
    accelerator: str | None = None,
    accelerator_count: int = 1,
    service_account_email: str | None = None,
) -> bool:
    """Provision a non-agent VM that runs a caller-supplied startup script.

    This reuses the shared GCP fallback-zone, secret-free boot, and teardown
    plumbing already used by agent VMs while keeping cloud imports hidden from
    the higher-level compute-job service layer. When ``accelerator`` is provided
    (a supported single-GPU short name or GCP type string) the VM is provisioned
    as a GPU instance — GPU-availability zones, guest accelerator, driver image,
    and TERMINATE host-maintenance — instead of a plain CPU VM.

    ``service_account_email`` attaches a narrower leaf identity to the VM (e.g.
    the image-builder SA for an untrusted off-orchestrator docker build). Absent
    ⇒ the orchestrator agent SA, unchanged for every existing caller.
    """
    _ensure_image_family_valid()
    if accelerator is not None:
        _ensure_gpu_image_family_valid()
    if LOCAL_MODE:
        raise RuntimeError(
            "provision_ephemeral_vm is unavailable in LOCAL_MODE; use the local backend"
        )
    return await _provision_gcp(
        vm_instance_id=vm_instance_id,
        task_id=None,
        provisioning_model=provisioning_model,
        startup_script_override=startup_script,
        machine_type=machine_type,
        accelerator=accelerator,
        accelerator_count=accelerator_count,
        service_account_email=service_account_email,
    )


def get_vm_serial_output(vm_instance_id: str, zone: str | None = None) -> str | None:
    """Best-effort serial-port tail for a VM.

    ``zone`` is optional for backward compatibility. Callers that persisted the
    zone durably (for example compute jobs) should pass it explicitly so serial
    recovery stays correct across leader restarts when ``_vm_zones`` is empty.
    """
    zone = zone or get_vm_zone(vm_instance_id)
    if not zone or zone == "unknown":
        return None
    return _capture_serial_for_vm(vm_instance_id, zone)


# ─── Shared VM Helpers (used by both delete and cleanup paths) ────────────


def _capture_serial_for_vm(vm_name: str, zone: str) -> str | None:
    """Best-effort serial capture. Returns tail or None.

    8000-byte tail (was 2000): the script's death window is often
    minutes before the reaper acts. The last 500-2000 bytes are
    routine systemd housekeeping (cert refresh, session activity)
    that came AFTER the script died — the actually-useful lines
    (BEACON_*, FATAL, materialize stderr, kernel OOM) sit further
    back. 8KB is large enough to hold the full startup-script
    journal output for the longest paths we've seen, while still
    fitting in JSONB without bloating the row.
    """
    from google.cloud import compute_v1

    if not zone or zone == "unknown":
        logger.warning(f"[COMPUTE] Serial capture skipped for {vm_name}: zone='{zone}'")
        return None
    try:
        client = compute_v1.InstancesClient()
        serial = client.get_serial_port_output(
            project=GCP_PROJECT_ID, zone=zone, instance=vm_name, timeout=10
        )
        return (serial.contents or "")[-8000:]
    except Exception as e:
        # Serial capture is the only forensic source for agent startup failures;
        # when it fails we must know why (VM already gone vs permission vs quota).
        logger.warning(
            "[COMPUTE] _capture_serial_for_vm failed for vm=%s zone=%s: %s: %s",
            vm_name,
            zone,
            type(e).__name__,
            e,
        )
        return None


def get_vm_status(vm_name: str, zone: str) -> str | None:
    """Return GCE instance status (e.g. RUNNING, TERMINATED, STOPPING) or None
    if the VM doesn't exist / API failed.

    Used by reaper to distinguish spot preemption (TERMINATED) from real agent
    crash (RUNNING but heartbeat stale). Cheap GCE API call, not async — wrap
    in asyncio.to_thread when called from the reaper.
    """
    from google.cloud import compute_v1

    if not zone or zone == "unknown":
        return None
    try:
        client = compute_v1.InstancesClient()
        instance = client.get(
            project=GCP_PROJECT_ID, zone=zone, instance=vm_name, timeout=10
        )
        return getattr(instance, "status", None) or None
    except Exception as e:
        if "was not found" in str(e) or "404" in str(e):
            # VM already gone — Spot DELETE termination action means preempted
            # instances are deleted, not just stopped. Return a synthetic
            # "DELETED" so callers can distinguish "vanished" from "API errored".
            return "DELETED"
        logger.debug(
            f"[COMPUTE] get_vm_status failed for vm={vm_name} zone={zone}: {e}"
        )
        return None


def get_vm_created_at(vm_name: str, zone: str) -> datetime | None:
    """Return a VM's GCE creation timestamp as a naive UTC datetime."""
    from google.cloud import compute_v1

    if not zone or zone == "unknown":
        return None
    try:
        client = compute_v1.InstancesClient()
        instance = client.get(project=GCP_PROJECT_ID, zone=zone, instance=vm_name)
    except Exception:
        logger.warning(
            "[COMPUTE] get_vm_created_at failed for vm=%s zone=%s",
            vm_name,
            zone,
            exc_info=True,
        )
        return None

    created_at = getattr(instance, "creation_timestamp", None)
    if not created_at:
        return None
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        logger.warning(
            "[COMPUTE] could not parse creation_timestamp for vm=%s zone=%s: %r",
            vm_name,
            zone,
            created_at,
        )
        return None


def _delete_single_vm(vm_name: str, zone: str) -> bool:
    """Delete a single VM. No serial capture. Returns True on success."""
    from google.cloud import compute_v1

    try:
        client = compute_v1.InstancesClient()
        operation = client.delete(project=GCP_PROJECT_ID, zone=zone, instance=vm_name)
        operation.result(timeout=300)
        return True
    except Exception as e:
        if "was not found" in str(e) or "404" in str(e):
            return True  # Already gone
        logger.exception(f"[VM DELETE] Failed to delete {vm_name} in {zone}")
        return False


async def stop_vm(vm_instance_id: str, *, reason: str) -> bool:
    """Best-effort graceful stop for a Blueprint-managed GCE VM.

    Used by compute-job teardown before delete so the guest has a window to run
    its shutdown trap and flush durable artifacts.
    """

    def _find_and_stop():
        from google.cloud import compute_v1

        instance_client = compute_v1.InstancesClient()
        zones_to_try = []
        with _zone_lock:
            known_zone = _vm_zones.get(vm_instance_id)
        if known_zone:
            zones_to_try.append(known_zone)
        zones_to_try.extend(z for z in _all_managed_zones() if z not in zones_to_try)

        for zone in zones_to_try:
            try:
                instance = instance_client.get(
                    project=GCP_PROJECT_ID, zone=zone, instance=vm_instance_id
                )
                status = getattr(instance, "status", "") or ""
                if status in ("TERMINATED", "STOPPING"):
                    return True
                operation = instance_client.stop(
                    project=GCP_PROJECT_ID, zone=zone, instance=vm_instance_id
                )
                operation.result(timeout=180)
                logger.info(
                    "[VM STOP] Successfully stopped %s in %s for %s",
                    vm_instance_id,
                    zone,
                    reason,
                )
                return True
            except Exception as e:
                if "was not found" in str(e) or "404" in str(e):
                    continue
                raise
        logger.warning("[VM STOP] %s not found in any zone", vm_instance_id)
        return True

    try:
        async with _gcp_ops_semaphore:
            return await asyncio.wait_for(
                asyncio.to_thread(_find_and_stop),
                timeout=210,
            )
    except asyncio.TimeoutError:
        logger.warning("[VM STOP] Timeout stopping VM %s (>210s)", vm_instance_id)
        return False
    except Exception as e:
        from google.api_core.exceptions import GoogleAPICallError, RetryError

        if isinstance(e, (RetryError, GoogleAPICallError)):
            logger.warning(
                "[VM STOP] Failed to stop VM %s: GCE API transient: %s: %s",
                vm_instance_id,
                type(e).__name__,
                e,
            )
        else:
            logger.warning(
                "[VM STOP] Failed to stop VM %s", vm_instance_id, exc_info=True
            )
        return False


def _list_vms_in_zone(zone: str) -> list[tuple[str, str, str]]:
    """List all Blueprint-managed instances in a single zone.

    Returns [(name, zone, status)] for both ``agent-vm-*`` (task agent VMs) and
    ``compute-job-vm-*`` (budgeted compute-job VMs). Without the latter prefix a
    compute-job VM that survived its immediate delete retries would leak forever,
    since the orphan sweep would never inventory it.
    """
    from google.cloud import compute_v1

    results = []
    try:
        client = compute_v1.InstancesClient()
        # NB: the Compute v1 REST API does NOT support gcloud's `name:prefix`
        # substring operator — gcloud applies it client-side. Passing it as a
        # server-side filter silently returns an empty page (interpreted as a
        # nonsensical map-key query on a string field), so cleanup runs forever
        # finding "0 orphans". Fetch unfiltered and prefix-match in Python.
        request = compute_v1.ListInstancesRequest(project=GCP_PROJECT_ID, zone=zone)
        # Per-RPC timeout so a single unresponsive zone can't freeze the whole cleanup cycle.
        results = [
            (instance.name, zone, instance.status)
            for instance in client.list(request=request, timeout=30)
            if instance.name.startswith(_MANAGED_VM_PREFIXES)
        ]
    except Exception:
        logger.exception(f"[GCP CLEANUP] Failed to list VMs in zone {zone}")
    return results


# ─── Individual VM Deletion ───────────────────────────────────────────────


async def _delete_gcp(vm_instance_id: str):
    """Delete a single VM. Captures serial BEFORE deletion for reliable forensics."""
    logger.info(f"Issuing external kill command for VM: {vm_instance_id}")

    def _find_capture_and_delete():
        from google.cloud import compute_v1

        instance_client = compute_v1.InstancesClient()
        zones_to_try = []
        with _zone_lock:
            known_zone = _vm_zones.pop(vm_instance_id, None)
        if known_zone:
            zones_to_try.append(known_zone)
        zones_to_try.extend(z for z in _all_managed_zones() if z not in zones_to_try)

        for zone in zones_to_try:
            try:
                instance = instance_client.get(
                    project=GCP_PROJECT_ID, zone=zone, instance=vm_instance_id
                )
                # Serial capture only makes sense while the agent was running — for already
                # TERMINATED/STOPPING VMs the buffer is just shutdown output, and the 10s call
                # eats into the delete budget for no forensic gain.
                status = getattr(instance, "status", "") or ""
                if status in ("TERMINATED", "STOPPING"):
                    serial_tail = None
                else:
                    serial_tail = _capture_serial_for_vm(vm_instance_id, zone)
                operation = instance_client.delete(
                    project=GCP_PROJECT_ID, zone=zone, instance=vm_instance_id
                )
                operation.result(timeout=180)
                logger.info(f"Successfully terminated VM {vm_instance_id} in {zone}")
                return zone, serial_tail
            except Exception as e:
                if "was not found" in str(e) or "404" in str(e):
                    continue
                raise
        logger.warning(f"VM {vm_instance_id} not found in any zone")
        return None, None

    try:
        async with _gcp_ops_semaphore:
            # Budget: up to ~10s zone probes + up to 10s serial + up to 180s delete op.
            # The prior 60s cap left ops that actually reached GCP stranded mid-delete.
            _deleted_zone, serial_tail = await asyncio.wait_for(
                asyncio.to_thread(_find_capture_and_delete), timeout=240
            )
        _clear_vm_heartbeat_state(vm_instance_id)

        if serial_tail:
            logger.info(f"[VM-SERIAL] {vm_instance_id} last output:\n{serial_tail}")

        # Best-effort: clean up any leftover credentials secret
        async with _gcp_ops_semaphore:
            await asyncio.to_thread(_delete_agent_secret, vm_instance_id)
        return True
    except asyncio.TimeoutError:
        logger.warning(f"Timeout deleting VM {vm_instance_id} (>240s)")
        return False
    except Exception as e:
        # GCE delete-op polling can exhaust retries on transient backend errors
        # (zone-operations.get returning 5xx, gRPC unavailable, etc.). The
        # delete request was already issued; the next reaper cycle re-detects
        # and retries if the VM is still alive. Demote these to a one-line
        # warning — the full traceback is noise in the dashboard.
        from google.api_core.exceptions import (
            GoogleAPICallError,
            RetryError,
        )

        if isinstance(e, (RetryError, GoogleAPICallError)):
            logger.warning(
                f"Failed to terminate VM {vm_instance_id}: "
                f"GCE API transient: {type(e).__name__}: {e}"
            )
        else:
            logger.warning(f"Failed to terminate VM {vm_instance_id}", exc_info=True)
        return False


# ─── Bulk Orphan Cleanup ──────────────────────────────────────────────────


class CleanupResult:
    """Structured result from orphan cleanup."""

    __slots__ = ("agent_vm_count", "deleted", "failed", "orphan_count", "total_gcp_vms")

    def __init__(self):
        self.total_gcp_vms: int = 0
        self.agent_vm_count: int = 0
        self.orphan_count: int = 0
        self.deleted: list[str] = []
        self.failed: list[str] = []


async def _refresh_gcp_cleanup_active_vm_ids() -> set[str]:
    """Re-read the live exemption set immediately before deleting orphans."""
    from app.services import _collect_gcp_cleanup_active_vm_ids

    async with AsyncSessionLocal() as session:
        return await _collect_gcp_cleanup_active_vm_ids(session)


async def _cleanup_gcp(active_vm_ids: list[str]) -> CleanupResult:
    """Scan all zones for orphaned VMs and delete them.

    Three-phase approach:
    1. Parallel zone listing
    2. Classify orphans (set lookup)
    3. Parallel deletion (no serial capture for TERMINATED)
    """
    result = CleanupResult()
    active_set = set(active_vm_ids)

    # Phase 1: Parallel zone listing (semaphore-bounded)
    # Use _all_managed_zones() so GPU compute-job VMs in accelerator-specific
    # zones (e.g. us-east1-d, us-east4-c) are included in the orphan sweep.
    unique_zones = list(dict.fromkeys(_all_managed_zones()))
    logger.info(
        f"[GCP CLEANUP] Cycle starting — scanning {len(unique_zones)} zones, DB active: {len(active_vm_ids)}"
    )

    async def _list_zone_bounded(zone: str):
        async with _gcp_ops_semaphore:
            return await asyncio.to_thread(_list_vms_in_zone, zone)

    list_tasks = [_list_zone_bounded(zone) for zone in unique_zones]
    zone_results = await asyncio.gather(*list_tasks, return_exceptions=True)

    # Phase 2: Classify
    all_vms: list[tuple[str, str, str]] = []
    for zr in zone_results:
        if isinstance(zr, Exception):
            logger.error(f"[GCP CLEANUP] Zone listing failed: {zr}")
            continue
        all_vms.extend(zr)

    result.total_gcp_vms = len(all_vms)
    result.agent_vm_count = sum(
        1 for name, _, _ in all_vms if name.startswith("agent-vm-")
    )
    orphans = [
        (name, zone, status) for name, zone, status in all_vms if name not in active_set
    ]
    result.orphan_count = len(orphans)

    logger.info(
        f"[GCP CLEANUP] GCP inventory: {result.total_gcp_vms} VMs, "
        f"DB active: {len(active_vm_ids)}, orphans: {result.orphan_count}"
    )

    if not orphans:
        return result

    # Revalidate the classification right before deleting anything so a VM that
    # just became active after the inventory snapshot is not reaped by stale
    # state from the start of the sweep.
    fresh_active_set = await _refresh_gcp_cleanup_active_vm_ids()
    if fresh_active_set != active_set:
        refreshed_orphans = [
            (name, zone, status)
            for name, zone, status in orphans
            if name not in fresh_active_set
        ]
        if len(refreshed_orphans) != len(orphans):
            logger.info(
                "[GCP CLEANUP] Revalidated before delete — orphans %d -> %d",
                len(orphans),
                len(refreshed_orphans),
            )
        orphans = refreshed_orphans
        result.orphan_count = len(orphans)

    if not orphans:
        return result

    # Phase 3: Parallel deletion (no blocking serial capture).
    # _format_orphan_status tags TERMINATED+SPOT VMs as "(likely spot
    # preemption)" so operators distinguish capacity-driven kills from
    # agent crashes when scanning logs.
    async def _delete_orphan(name: str, zone: str, status: str):
        logger.warning(
            f"[GCP CLEANUP] Deleting orphan {name} ({zone}, {_format_orphan_status(status)})"
        )
        try:
            async with _gcp_ops_semaphore:
                success = await asyncio.to_thread(_delete_single_vm, name, zone)
            if success:
                result.deleted.append(name)
                # Best-effort secret cleanup
                try:
                    async with _gcp_ops_semaphore:
                        await asyncio.to_thread(_delete_agent_secret, name)
                except Exception:
                    logger.exception(
                        f"[GCP CLEANUP] Failed to clean up secret for orphan {name}"
                    )
            else:
                result.failed.append(name)
        except asyncio.TimeoutError:
            result.failed.append(name)
            logger.exception(f"[GCP CLEANUP] Timeout deleting orphan {name}")
        except Exception:
            result.failed.append(name)
            logger.exception(f"[GCP CLEANUP] Exception deleting orphan {name}")

    delete_tasks = [
        _delete_orphan(name, zone, status) for name, zone, status in orphans
    ]
    await asyncio.gather(*delete_tasks, return_exceptions=True)

    # Adjust agent_vm_count to reflect post-deletion inventory so the
    # orchestrator dispatch headroom is freed immediately rather than waiting
    # for the next cache TTL cycle.
    deleted_agent_vms = sum(1 for n in result.deleted if n.startswith("agent-vm-"))
    result.agent_vm_count = max(0, result.agent_vm_count - deleted_agent_vms)

    if result.deleted or result.failed:
        logger.info(
            f"[GCP CLEANUP] Done: {len(result.deleted)} deleted, "
            f"{len(result.failed)} failed out of {result.orphan_count} orphans"
        )

    return result


# ─── Zone Health API ───────────────────────────────────────────────────────


def get_zone_health() -> dict:
    """Return zone health data for admin API consumption."""
    result = {}
    with _zone_lock:
        for zone, _ in FALLBACK_ZONES:
            data = _zone_health.get(zone, {})
            cooldown_active = False
            cooldown_expires = data.get("cooldown_expires_at")
            if cooldown_expires:
                cooldown_active = datetime.now(timezone.utc) < cooldown_expires

            consecutive = data.get("consecutive_failures", 0)
            if _zone_in_disabled_region(zone):
                status = "disabled"
            elif consecutive == 0:
                status = "healthy"
            elif cooldown_active:
                status = "exhausted"
            else:
                status = "recovering"

            outcomes = data.get("recent_outcomes")
            provision_count = len(outcomes) if outcomes else 0
            success_rate = (
                (sum(outcomes) / provision_count) if provision_count else None
            )

            result[zone] = {
                "status": status,
                "last_success": data["last_success"].isoformat()
                if data.get("last_success")
                else None,
                "last_failure": data["last_failure"].isoformat()
                if data.get("last_failure")
                else None,
                "last_failure_reason": data.get("last_failure_reason"),
                "consecutive_failures": consecutive,
                "cooldown_active": cooldown_active,
                "cooldown_expires_at": cooldown_expires.isoformat()
                if cooldown_active and cooldown_expires
                else None,
                "success_rate": success_rate,
                "provision_count": provision_count,
            }
    return result


def get_zone_health_summary() -> dict:
    """Return a compact summary for health endpoints."""
    health = get_zone_health()
    exhausted = [z for z, d in health.items() if d["status"] == "exhausted"]
    disabled = [z for z, d in health.items() if d["status"] == "disabled"]
    zone_success_rates = {
        z: d["success_rate"] for z, d in health.items() if d["success_rate"] is not None
    }
    active_total = len(FALLBACK_ZONES) - len(disabled)
    quota = get_quota_cooldowns()
    return {
        "total_zones": len(FALLBACK_ZONES),
        "active_zones": active_total,
        "disabled": disabled,
        "available": active_total - len(exhausted),
        "exhausted": exhausted,
        "cooldown_recovery": {
            z: health[z].get("cooldown_expires_at")
            for z in exhausted
            if health[z].get("cooldown_expires_at")
        },
        "zone_success_rates": zone_success_rates,
        # Quota cooldowns are NOT zone marks: when these are active the zones
        # above can all look healthy while provisioning is paused project-wide
        # (global) or per-region. Surfaced so "all zones healthy but nothing
        # dispatches" is explainable from the health endpoint alone.
        "global_quota_cooldown_until": quota["global_quota_cooldown_until"],
        "region_quota_cooldowns": quota["region_quota_cooldowns"],
    }


def any_zone_available() -> bool:
    """Return True if at least one ACTIVE zone is not blocked.

    A zone is blocked by its own failure cooldown, by a quota cooldown on its
    region, or by the project-global quota cooldown (which blocks every zone).
    Disabled zones are excluded — they have no subnet provisioned and cannot
    accept VMs even if not in cooldown.
    """
    now = datetime.now(timezone.utc)
    with _zone_lock:
        for zone, _ in _active_fallback_zones():
            if _zone_blocked_until(zone, now) is None:
                return True
    return False


# Failure reasons that are AUTHORITATIVE zone-level evidence (the GCP API
# itself reported a zone-scoped problem) — these charge the zone immediately.
# Everything else reaching record_zone_failure (HEARTBEAT_STARVATION,
# AGENT_STARTUP_FAILURE, PROVISION_INLINE_FAILURE, …) describes a VM that the
# zone provisioned SUCCESSFULLY but whose agent then died silently — usually a
# VM/image/startup-script/account failure, not zone health — so it only counts
# toward the zone once corroborated by repeated incidents in a short window.
_ZONE_AUTHORITATIVE_FAILURE_REASONS = frozenset(
    {
        "RESOURCE_POOL_EXHAUSTED",
        "RESOURCE_UNAVAILABLE",
        "SUBNET_NOT_FOUND",
    }
)
ZONE_SUSPECT_CORROBORATION_THRESHOLD = int(
    os.getenv("ZONE_SUSPECT_CORROBORATION_THRESHOLD", "3")
)
ZONE_SUSPECT_WINDOW_SECONDS = int(os.getenv("ZONE_SUSPECT_WINDOW_SECONDS", "1800"))
# zone -> deque[datetime] of recent uncorroborated VM-death reports
# (guarded by _zone_lock; bounded by maxlen + window pruning)
_zone_suspect_events: dict[str, _deque] = {}


def record_zone_failure(zone: str, reason: str = "HEARTBEAT_STARVATION") -> None:
    """Record a zone failure from outside the provisioning path (e.g. reaper).

    Called when a VM was provisioned in a zone but the agent died without
    heartbeating. The GCP insert demonstrably SUCCEEDED in that zone, so a
    single silent death is weak evidence about zone health — it is far more
    often a VM/image/startup-script/account failure. Charging each one
    directly poisoned zone cooldowns and success rates with non-zone faults.

    Reasons outside ``_ZONE_AUTHORITATIVE_FAILURE_REASONS`` are therefore
    treated as *suspect* evidence: the zone is only charged (cooldown +
    success-rate hit) once ``ZONE_SUSPECT_CORROBORATION_THRESHOLD`` reports
    land in the same zone within ``ZONE_SUSPECT_WINDOW_SECONDS``; the charge
    consumes the accumulated reports so the next charge needs fresh
    corroboration. Authoritative zone-scoped reasons charge immediately.
    """
    with _zone_lock:
        now_fail = datetime.now(timezone.utc)
        suspect_count = None
        if reason not in _ZONE_AUTHORITATIVE_FAILURE_REASONS:
            events = _zone_suspect_events.setdefault(zone, _deque(maxlen=64))
            cutoff = now_fail - timedelta(seconds=ZONE_SUSPECT_WINDOW_SECONDS)
            while events and events[0] < cutoff:
                events.popleft()
            events.append(now_fail)
            suspect_count = len(events)
            if suspect_count < ZONE_SUSPECT_CORROBORATION_THRESHOLD:
                charged = False
            else:
                # Corroborated — consume the evidence and charge the zone once.
                events.clear()
                reason = f"{reason}_CORROBORATED"
                charged = True
        else:
            charged = True

        if charged:
            entering_cooldown = zone not in _zone_failures
            _zone_failures[zone] = now_fail
            health = _zone_health.setdefault(zone, {})
            old_cf = health.get("consecutive_failures", 0)
            last_fail_time = health.get("last_failure")
            # Debounce parallel failures (same logic as inline provisioning handler)
            within_debounce = (
                last_fail_time is not None
                and (now_fail - last_fail_time).total_seconds() < ZONE_COOLDOWN_SECONDS
            )
            new_failure_count = old_cf if within_debounce else old_cf + 1
            health["last_failure"] = now_fail
            health["consecutive_failures"] = new_failure_count
            health["last_failure_reason"] = reason
            cooldown_secs = _zone_cooldown_seconds(new_failure_count)
            health["cooldown_expires_at"] = now_fail + timedelta(seconds=cooldown_secs)
            outcomes = health.setdefault(
                "recent_outcomes", _deque(maxlen=ZONE_HISTORY_WINDOW)
            )
            outcomes.append(False)

    if not charged:
        logger.info(
            f"Zone {zone}: suspect VM-death report ({reason}) "
            f"{suspect_count}/{ZONE_SUSPECT_CORROBORATION_THRESHOLD} within "
            f"{ZONE_SUSPECT_WINDOW_SECONDS}s — not charging zone health "
            f"(likely VM/image/startup fault, not a zone fault)"
        )
        return

    if entering_cooldown:
        logger.warning(
            f"Zone {zone} entering cooldown (from reaper): {new_failure_count} consecutive failure(s), "
            f"reason={reason}, cooldown={cooldown_secs}s"
        )


def soonest_zone_recovery() -> datetime | None:
    """Return the earliest ACTIVE zone unblock time, or None if a zone is available.

    Accounts for per-zone failure cooldowns, per-region quota cooldowns, and
    the project-global quota cooldown (a zone blocked by its region's or the
    project's quota is NOT available even with a clean zone-failure record).
    Disabled zones are excluded — they have no subnet so a "recovered" cooldown
    in a disabled region wouldn't unlock spawning anyway.
    """
    now = datetime.now(timezone.utc)
    soonest = None
    with _zone_lock:
        for zone, _ in _active_fallback_zones():
            blocked_until = _zone_blocked_until(zone, now)
            if blocked_until is None:
                return None  # Available now
            if soonest is None or blocked_until < soonest:
                soonest = blocked_until
    return soonest


def zone_recovery_wait_seconds() -> float | None:
    """Seconds until the soonest zone recovery, or None if a zone is available now.

    Audit C10: ``soonest_zone_recovery()`` returns a tz-AWARE datetime
    (``cooldown_expires_at`` is built from ``datetime.now(timezone.utc)``), so
    the subtraction MUST use an aware now. Callers previously subtracted the
    app's naive ``_utcnow()`` here, which raised ``TypeError`` during zone
    exhaustion — silently rolling back the infra-requeue's QUEUED/retry_after
    state and spamming the all-zones-cooldown error counter. Centralising the
    aware subtraction in one helper removes that naive/aware footgun for every
    caller. Result is floored at 0 so a just-expired cooldown never returns
    negative.
    """
    recovery = soonest_zone_recovery()
    if recovery is None:
        return None
    return max(0.0, (recovery - datetime.now(timezone.utc)).total_seconds())
