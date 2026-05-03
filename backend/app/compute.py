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
    ("us-east4-b",    "agent-sandbox-subnet-us-east4"),
    ("us-east4-a",    "agent-sandbox-subnet-us-east4"),
    ("us-east1-c",    "agent-sandbox-subnet-us-east1"),
    ("us-east1-b",    "agent-sandbox-subnet-us-east1"),
    ("us-west1-b",    "agent-sandbox-subnet-us-west1"),
    ("us-west1-a",    "agent-sandbox-subnet-us-west1"),
]

# Patterns indicating infrastructure-wide provision failure (not task-specific).
# Used by orchestrator and reaper to avoid burning retry budgets for transient outages.
INFRA_PROVISION_PATTERNS = [
    "all zones in cooldown", "all zones exhausted",
    "global provision capacity",
    "zone_resource_pool_exhausted", "quota", "cpus_all_regions",
    "does not have enough resources", "in_use_addresses",
    "503 service unavailable",
]

# Track which zone each VM was created in (for delete/cleanup)
_vm_zones: dict[str, str] = {}

# Semaphores to cap concurrent GCP API calls — prevents thread pool starvation
# and stays well under GCP's 20 QPS rate limit.
# Separate limits for slow insert (up to 300s) vs fast ops (list/delete/get, ~10s)
# so long-running provisions don't starve orphan cleanup.
# Local semaphore still used as secondary guard even with Redis (prevents thread starvation).
_MAX_GLOBAL_PROVISIONS = int(os.getenv('MAX_PROVISION_SLOTS', '10'))
_gcp_provision_semaphore = asyncio.Semaphore(_MAX_GLOBAL_PROVISIONS)  # insert/create — long-running (up to 300s)
_gcp_ops_semaphore = asyncio.Semaphore(8)         # list/delete/get — fast ops (~10s)

# Distributed provision semaphore via Redis sorted set (falls back to local-only)
_PROVISION_REDIS_KEY = "blueprint:active-provisions"
_PROVISION_SLOT_TTL = 600  # 10 min — auto-reclaim if process crashes


async def _acquire_provision_slot(vm_id: str) -> bool:
    """Acquire a global provision slot via Redis. Falls back to local semaphore check."""
    from app.redis_client import get_redis
    r = await get_redis()
    if not r:
        return True  # No Redis — rely on local semaphore only
    try:
        now = time.time()
        pipe = r.pipeline()
        pipe.zremrangebyscore(_PROVISION_REDIS_KEY, 0, now)  # Clean expired
        pipe.zcard(_PROVISION_REDIS_KEY)
        results = await pipe.execute()
        count = results[1]
        if count >= _MAX_GLOBAL_PROVISIONS:
            return False
        await r.zadd(_PROVISION_REDIS_KEY, {vm_id: now + _PROVISION_SLOT_TTL})
        return True
    except Exception as e:
        logger.warning(f"[REDIS] Provision slot acquire failed, using local semaphore: {e}")
        return True  # Degrade gracefully


async def _release_provision_slot(vm_id: str):
    """Release a global provision slot."""
    from app.redis_client import get_redis
    r = await get_redis()
    if r:
        try:
            await r.zrem(_PROVISION_REDIS_KEY, vm_id)
        except Exception:
            logger.exception(f"[COMPUTE] Failed to release provision slot for {vm_id} (TTL will reclaim)")

# Zone cooldown: skip zones that recently failed (avoid wasting time retrying exhausted zones)
_zone_failures: dict[str, datetime] = {}
ZONE_COOLDOWN_SECONDS = int(os.getenv('ZONE_COOLDOWN_BASE_SECONDS', '180'))  # 3 minutes — base for exponential backoff
ZONE_RECOVERY_WAIT_CAP_SECONDS = int(os.getenv('ZONE_RECOVERY_WAIT_CAP_SECONDS', '300'))  # Max wait when all zones in cooldown


ZONE_HISTORY_WINDOW = int(os.getenv('ZONE_HISTORY_WINDOW', '20'))  # Rolling window size for success rate
_ZONE_NEUTRAL_RATE = 0.5  # Default success rate for zones with no history


def _zone_cooldown_seconds(consecutive_failures: int) -> int:
    """Exponential backoff: 3min → 6min → 12min → 24min → 30min cap."""
    return min(ZONE_COOLDOWN_SECONDS * (2 ** consecutive_failures), 1800)

# Zone health tracking: populated by provisioning attempts
_zone_health: dict[str, dict] = {}
# Structure: {
#   "us-central1-f": {
#     "last_success": datetime, "last_failure": datetime,
#     "consecutive_failures": 0, "last_failure_reason": str,
#     "recent_outcomes": deque[bool],  # True=success, False=failure, maxlen=ZONE_HISTORY_WINDOW
#   }
# }

def _zone_success_rate(zone: str) -> float:
    """Return the recent success rate for a zone (0.0–1.0).

    Reads the rolling ``recent_outcomes`` deque stored in ``_zone_health``.
    Returns the neutral rate (0.5) when no history exists so unknown zones
    sort in the middle, neither preferred nor penalised.
    Caller is responsible for holding ``_zone_lock`` when thread safety matters.
    """
    outcomes = _zone_health.get(zone, {}).get("recent_outcomes")
    if not outcomes:
        return _ZONE_NEUTRAL_RATE
    return sum(outcomes) / len(outcomes)


# Thread lock for zone state dicts (modified from asyncio.to_thread workers, read from event loop)
_zone_lock = threading.RLock()
MACHINE_TYPE = os.getenv("MACHINE_TYPE", "e2-standard-2")

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
    region = "-".join(zone.split("-")[:-1])  # us-central1-f → us-central1
    return region in _DISABLED_REGIONS


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
AGENT_IMAGE_FAMILY = os.getenv("AGENT_IMAGE_FAMILY", f"projects/{GCP_PROJECT_ID}/global/images/family/blueprint-agent-v2")

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
    "vpm-worker@your-gcp-project-id.iam.gserviceaccount.com"
)

# Agent source directory for local mode
AGENT_DIR = os.getenv("AGENT_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "agent"))

def _validate_env_config() -> None:
    """Fail fast if placeholder env vars are still set."""
    if GCP_PROJECT_ID == "your-gcp-project-id":
        raise RuntimeError("GCP_PROJECT_ID is not configured (still set to placeholder)")
    if ORCHESTRATOR_URL == "http://localhost:8080" and not LOCAL_MODE:
        raise RuntimeError("ORCHESTRATOR_URL is not configured (still set to localhost)")
    if "your-gcp-project-id" in AGENT_SERVICE_ACCOUNT_EMAIL and not LOCAL_MODE:
        raise RuntimeError("AGENT_SERVICE_ACCOUNT_EMAIL is not configured (still contains placeholder)")


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
        logger.info(f"[STARTUP] Image family '{family_name}' validated in project '{project}'")
    except Exception as e:
        logger.exception("[STARTUP] Image family validation failed")
        raise RuntimeError(f"AGENT_IMAGE_FAMILY '{AGENT_IMAGE_FAMILY}' not found: {e}")

_image_family_valid: bool | None = None  # None = not yet checked, True/False = result
_image_family_last_check: float = 0.0
_IMAGE_FAMILY_RECHECK_INTERVAL: float = 300.0  # Retry validation every 5 min if failed

if not LOCAL_MODE and os.getenv("K_REVISION"):  # K_REVISION set by Cloud Run — skip in dev/test
    _validate_env_config()
    try:
        _validate_image_family()
        _image_family_valid = True
        _image_family_last_check = time.time()
    except Exception:
        _image_family_valid = False
        _image_family_last_check = time.time()
        logger.exception(f"[STARTUP] Image family validation FAILED — will retry in {_IMAGE_FAMILY_RECHECK_INTERVAL}s")

# Manifest builder + payload URI validator are imported at the top of this
# module so they're re-exported for existing `from app.compute import ...`
# callers (and tests that mock via `app.compute.X`).
_REEXPORTS = (validate_payload_uri, build_agent_secret_manifest)  # keep referenced so linters don't strip the re-exports

ARTIFACT_BUCKET_NAME = os.getenv("ARTIFACT_BUCKET_NAME", "blueprint-agent-artifacts")


# --- Local Mode: Track subprocesses ---
_local_processes: dict[str, subprocess.Popen] = {}


async def prepare_and_store_agent_secret(
    vm_instance_id: str, task_id: uuid.UUID,
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


async def provision_agent_vm(vm_instance_id: str, task_id: uuid.UUID, payload_uri: str | None = None,
                              oauth_token: str | None = None, claude_config_json: str | None = None,
                              task_type: str = "", secret_version_name: str | None = None,
                              provider: str | None = None, auth_mode: str | None = None,
                              model: str | None = None, execution_mode: str | None = None,
                              provider_metadata: dict | None = None,
                              provider_settings: dict | None = None):
    """
    Provisions an agent. In LOCAL_MODE, spawns a subprocess.
    In production, creates a GCP Compute Engine VM.
    oauth_token: Long-lived OAuth token (sk-ant-oat01-...) for Claude Code CLI.
    claude_config_json: Content of ~/.claude.json for onboarding bypass.
    secret_version_name: Pre-computed Secret Manager version (from prepare_and_store_agent_secret).
        If provided, skips the internal secret build+store step.
    """
    if payload_uri:
        validate_payload_uri(payload_uri)
    # Block provisioning if image family is invalid. Retry validation periodically
    # so a transient startup failure doesn't permanently disable provisioning.
    global _image_family_valid, _image_family_last_check
    if _image_family_valid is False and not LOCAL_MODE:
        now = time.time()
        if now - _image_family_last_check >= _IMAGE_FAMILY_RECHECK_INTERVAL:
            try:
                _validate_image_family()
                _image_family_valid = True
                _image_family_last_check = now
                logger.info("[COMPUTE] Image family re-validation succeeded — provisioning re-enabled")
            except Exception:
                _image_family_last_check = now
                logger.exception("[COMPUTE] Image family re-validation still failing")
                raise RuntimeError(
                    f"VM provisioning disabled: image family '{AGENT_IMAGE_FAMILY}' "
                    f"failed validation. Next retry in {_IMAGE_FAMILY_RECHECK_INTERVAL}s."
                )
        else:
            raise RuntimeError(
                f"VM provisioning disabled: image family '{AGENT_IMAGE_FAMILY}' "
                f"failed validation. Next retry in {_IMAGE_FAMILY_RECHECK_INTERVAL - (now - _image_family_last_check):.0f}s."
            )
    if LOCAL_MODE:
        return await _provision_local(
            vm_instance_id, task_id, payload_uri, oauth_token, claude_config_json,
            task_type=task_type, provider=provider, auth_mode=auth_mode, model=model,
            execution_mode=execution_mode, provider_metadata=provider_metadata,
            provider_settings=provider_settings,
        )
    return await _provision_gcp(vm_instance_id, task_id, payload_uri, oauth_token, claude_config_json,
                                task_type=task_type, secret_version_name=secret_version_name,
                                provider=provider, auth_mode=auth_mode, model=model,
                                execution_mode=execution_mode, provider_metadata=provider_metadata,
                                provider_settings=provider_settings)


_last_provision_error: _OrderedDict = _OrderedDict()
_PROVISION_ERROR_MAX = 500

def get_provision_error(vm_instance_id: str) -> str:
    """Get and clear the last provisioning error for a VM."""
    return _last_provision_error.pop(vm_instance_id, "")


def get_vm_zone(vm_instance_id: str) -> str:
    """Get the zone a VM was provisioned in."""
    return _vm_zones.get(vm_instance_id, "unknown")


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


async def cleanup_old_images(keep: int, family: str | None = None, project: str | None = None, dry_run: bool = False) -> dict:
    """Delete oldest custom images in `family`, retaining the `keep` most recent.

    GCP enforces a per-project image quota; without pruning, each Packer build leaves the
    previous images behind and eventually provisioning fails with quota errors. This function
    lists images in the family sorted by creationTimestamp and issues delete for everything
    past the `keep` boundary. Running VMs already booted from those images are unaffected —
    Compute Engine only references the source image at instance-create time.

    Returns a dict with: total (in family), deleted (count), kept, failed, dry_run.
    """
    if keep < 1:
        raise ValueError("keep must be >= 1 (must retain at least one image for VM provisioning)")
    if LOCAL_MODE:
        return {"total": 0, "deleted": 0, "kept": 0, "failed": [], "dry_run": dry_run, "skipped": "local_mode"}

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
        return {"total": total, "deleted": 0, "kept": total, "failed": [], "dry_run": dry_run}

    to_delete = images[keep:]
    failed: list[dict] = []

    if dry_run:
        return {
            "total": total, "deleted": 0, "kept": keep, "failed": [], "dry_run": True,
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

    return {"total": total, "deleted": deleted, "kept": keep, "failed": failed, "dry_run": False}


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
    logger.info(f"[LOCAL] Spawning agent subprocess: {vm_instance_id} (Task: {task_id})")
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
    env = {**os.environ, **_materialize_local_manifest(vm_instance_id, manifest)}
    env["PAYLOAD_URI"] = payload_uri or ""

    agent_dir = os.path.abspath(AGENT_DIR)
    if not os.path.isdir(agent_dir):
        logger.error(f"[LOCAL] Agent directory not found: {agent_dir}")
        return False

    try:
        proc = subprocess.Popen(
            ["npm", "start"],
            cwd=agent_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _local_processes[vm_instance_id] = proc
        logger.info(f"[LOCAL] Agent subprocess started (PID: {proc.pid})")

        # Start a background task to log output and clean up
        async def _monitor():
            try:
                await asyncio.to_thread(proc.wait)
            finally:
                _local_processes.pop(vm_instance_id, None)
                logger.info(f"[LOCAL] Agent subprocess exited (PID: {proc.pid}, code: {proc.returncode})")

        asyncio.create_task(_monitor())  # noqa: RUF006 — fire-and-forget monitor; lives for the subprocess lifetime
        return True
    except Exception:
        logger.exception("[LOCAL] Failed to spawn agent")
        return False


async def _delete_local(vm_instance_id: str):
    proc = _local_processes.pop(vm_instance_id, None)
    if proc and proc.poll() is None:
        logger.info(f"[LOCAL] Killing agent subprocess: {vm_instance_id} (PID: {proc.pid})")
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
            shutil.rmtree(local_root, ignore_errors=True)
    except Exception:
        logger.warning(f"[LOCAL] Failed to clean manifest files for {vm_instance_id}")
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
    Grants the agent service account secretAccessor on this specific secret.
    """
    from google.cloud import secretmanager
    from google.iam.v1 import policy_pb2

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
                    "labels": {"managed-by": "blueprint", "vm-instance": vm_instance_id},
                },
            }
        )
    except Exception as e:
        if "AlreadyExists" in str(type(e).__name__) or "409" in str(e):
            logger.info(f"Secret {secret_id} already exists — reusing")
            secret = type('Secret', (), {'name': secret_name})()
        else:
            raise

    # Add the secret version with the payload (overwrites any stale version)
    version = client.add_secret_version(
        request={
            "parent": secret.name,
            "payload": {"data": secret_payload.encode("utf-8")},
        }
    )

    # Grant the agent service account secretAccessor on this specific secret only.
    # Wrapped in exponential-backoff retry because concurrent provisions race on
    # IAM policy ETags and produce Aborted/409, which previously crashed VM
    # provisioning. Also short-circuits when the SA is already bound so reused
    # secrets don't accumulate duplicate bindings.
    from google.api_core.exceptions import Aborted
    agent_sa_member = f"serviceAccount:{AGENT_SERVICE_ACCOUNT_EMAIL}"
    max_retries = 5
    for attempt in range(max_retries):
        try:
            policy = client.get_iam_policy(request={"resource": secret.name})
            already_bound = any(
                b.role == "roles/secretmanager.secretAccessor"
                and agent_sa_member in b.members
                for b in policy.bindings
            )
            if already_bound:
                break
            binding = policy_pb2.Binding(
                role="roles/secretmanager.secretAccessor",
                members=[agent_sa_member],
            )
            policy.bindings.append(binding)
            client.set_iam_policy(request={"resource": secret.name, "policy": policy})
            break
        except Aborted:
            if attempt == max_retries - 1:
                logger.error(
                    "[COMPUTE] IAM policy ETag retry exhausted for %s after %d attempts",
                    secret.name, max_retries,
                )
                raise
            backoff_seconds = min(0.1 * (2 ** attempt), 2.0)
            logger.warning(
                "[COMPUTE] IAM policy ETag conflict on %s (attempt %d/%d), retrying in %.2fs",
                secret.name, attempt + 1, max_retries, backoff_seconds,
            )
            time.sleep(backoff_seconds)

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
                version.name, len(verification.payload.data), len(secret_payload),
            )
            raise RuntimeError(
                f"Secret {version.name} verification failed: payload mismatch"
            )
    except Exception as verify_err:
        logger.error(
            "[COMPUTE] Secret %s failed post-create verification: %s",
            version.name, verify_err,
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
    env = {str(key): str(value) for key, value in (manifest.get("env") or {}).items() if value is not None}
    for item in manifest.get("files", []) or []:
        original_path = str(item["path"])
        local_path = root / original_path.lstrip("/").replace("/", os.sep)
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
    for secret in client.list_secrets(request={"parent": parent, "filter": "labels.managed-by=blueprint"}):
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
    stats: dict[str, int] = {"listed": len(secrets), "deleted": 0, "skipped_young": 0, "skipped_active": 0, "failed": 0}
    for _secret_name, vm_id, create_time in secrets:
        if vm_id in active_vm_ids:
            stats["skipped_active"] += 1
            continue
        # Ensure create_time is timezone-aware for comparison
        ct = create_time if create_time.tzinfo else create_time.replace(tzinfo=timezone.utc)
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


async def _provision_gcp(vm_instance_id: str, task_id: uuid.UUID, payload_uri: str | None = None,
                         oauth_token: str | None = None, claude_config_json: str | None = None,
                         task_type: str = "", secret_version_name: str | None = None,
                         provider: str | None = None, auth_mode: str | None = None,
                         model: str | None = None, execution_mode: str | None = None,
                         provider_metadata: dict | None = None,
                         provider_settings: dict | None = None):
    from google.cloud import compute_v1

    logger.info(f"Starting provisioning for VM: {vm_instance_id} (Task: {task_id})")

    if secret_version_name is None:
        # Secret was not pre-computed by the caller — build and store it now.
        # (Callers that run upload_json_payload concurrently pass secret_version_name
        # from prepare_and_store_agent_secret to avoid this sequential fallback.)
        secret_version_name = await prepare_and_store_agent_secret(
            vm_instance_id, task_id, oauth_token, claude_config_json, task_type,
            provider=provider, auth_mode=auth_mode, model=model,
            execution_mode=execution_mode, provider_metadata=provider_metadata,
            provider_settings=provider_settings,
        )

    # payload_uri is validated upstream and safe to embed in the startup script
    import base64
    payload_uri_b64 = base64.b64encode(payload_uri.encode()).decode() if payload_uri else ""

    from app.compute_startup_script import build_agent_startup_script
    startup_script = build_agent_startup_script(
        task_id=str(task_id),
        secret_version_name=secret_version_name,
        payload_uri_b64=payload_uri_b64,
        orchestrator_url=ORCHESTRATOR_URL,
        materialize_manifest_b64=_MATERIALIZE_MANIFEST_B64,
    )

    instance_config = compute_v1.Instance()
    instance_config.name = vm_instance_id

    initialize_params = compute_v1.AttachedDiskInitializeParams()
    initialize_params.source_image = AGENT_IMAGE_FAMILY
    initialize_params.disk_size_gb = 50

    disk = compute_v1.AttachedDisk()
    disk.boot = True
    disk.auto_delete = True
    disk.initialize_params = initialize_params
    instance_config.disks = [disk]

    network_interface = compute_v1.NetworkInterface()
    network_interface.network = f"projects/{GCP_PROJECT_ID}/global/networks/{VPC_NETWORK}"
    # Default subnet — will be overridden per-zone in the fallback loop
    default_region = "-".join(GCP_ZONE.split("-")[:-1])
    network_interface.subnetwork = f"projects/{GCP_PROJECT_ID}/regions/{default_region}/subnetworks/{VPC_SUBNET}"
    instance_config.network_interfaces = [network_interface]

    metadata = compute_v1.Metadata()
    metadata_item = compute_v1.Items()
    metadata_item.key = "startup-script"
    metadata_item.value = startup_script
    serial_logging_item = compute_v1.Items()
    serial_logging_item.key = "serial-port-logging-enable"
    serial_logging_item.value = "TRUE"
    metadata.items = [metadata_item, serial_logging_item]
    instance_config.metadata = metadata

    service_account = compute_v1.ServiceAccount()
    service_account.email = AGENT_SERVICE_ACCOUNT_EMAIL
    # Security note: cloud-platform is the ONLY OAuth2 scope that covers Secret Manager.
    # There is no narrower secretmanager-specific scope. The actual security boundary is
    # the agent SA's IAM bindings (see infra/terraform/iam.tf):
    #   - ZERO project-level IAM roles
    #   - secretAccessor on its own specific secret only (granted per-VM in _store_agent_secret)
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
    if VM_PROVISIONING_MODEL == "SPOT":
        scheduling = compute_v1.Scheduling()
        scheduling.provisioning_model = "SPOT"
        # DELETE on preemption: Spot VMs we provision are single-task and can't
        # be restarted (the agent's startup-script work isn't idempotent against
        # mid-run interruption). Letting GCP DELETE them on preemption avoids
        # accumulating TERMINATED zombies that cleanup has to chase.
        scheduling.instance_termination_action = "DELETE"
        instance_config.scheduling = scheduling

    _cancel_event = threading.Event()

    def _insert_instance():
        instance_client = compute_v1.InstancesClient()
        last_error = None
        skipped_cooldown = []

        now_pre = datetime.now(timezone.utc)
        # _active_fallback_zones() filters out admin-disabled regions (DISABLED_REGIONS env)
        # so spawn attempts skip regions with no subnet provisioned.
        active_zones = _active_fallback_zones()
        healthy_zones = [(z, s) for z, s in active_zones if not (_zone_health.get(z, {}).get("cooldown_expires_at") and now_pre < _zone_health[z]["cooldown_expires_at"])]
        cooled_zones = [(z, s) for z, s in active_zones if (z, s) not in healthy_zones]
        # Sort healthy zones by recent success rate (descending) so well-performing
        # zones are preferred. Zones with no history get neutral rate 0.5.
        # Use stable sort with a small random tiebreak to distribute load across
        # equal-rate zones (e.g. all-new zones with 0.5 default).
        healthy_zones.sort(key=lambda zs: (_zone_success_rate(zs[0]), random.random()), reverse=True)
        zones_to_try = healthy_zones + cooled_zones

        for zone, subnet_name in zones_to_try:
            if _cancel_event.is_set():
                logger.info(f"[PROVISION] {vm_instance_id} cancelled — skipping remaining zones")
                return None
            now = datetime.now(timezone.utc)  # Refresh each iteration — zone attempts can take up to 300s
            # Skip zones in cooldown (recently failed) — use per-zone exponential backoff expiry
            zone_cooldown_expires = _zone_health.get(zone, {}).get("cooldown_expires_at")
            if zone_cooldown_expires and now < zone_cooldown_expires:
                skipped_cooldown.append(zone)
                continue

            try:
                # Update zone-dependent config for this attempt
                instance_config.machine_type = f"zones/{zone}/machineTypes/{MACHINE_TYPE}"
                zone_region = "-".join(zone.split("-")[:-1])
                network_interface.subnetwork = f"projects/{GCP_PROJECT_ID}/regions/{zone_region}/subnetworks/{subnet_name}"
                # Re-assign to ensure protobuf picks up the change
                instance_config.network_interfaces = [network_interface]

                # Delete any existing VM with this name first (stale from retries)
                try:
                    instance_client.get(project=GCP_PROJECT_ID, zone=zone, instance=vm_instance_id)
                    logger.info(f"Deleting stale VM {vm_instance_id} in {zone} before re-provisioning")
                    del_op = instance_client.delete(project=GCP_PROJECT_ID, zone=zone, instance=vm_instance_id)
                    del_op.result(timeout=300)
                except Exception as e:
                    if "was not found" not in str(e) and "404" not in str(e):
                        logger.warning(f"Failed to clean stale VM {vm_instance_id} in {zone}: {e}")

                operation = instance_client.insert(
                    project=GCP_PROJECT_ID,
                    zone=zone,
                    instance_resource=instance_config
                )
                # Inner wait is bounded below the outer 420s wait_for so we
                # don't routinely burn the full budget on a single zone.
                operation.result(timeout=240)

                with _zone_lock:
                    _vm_zones[vm_instance_id] = zone
                    health = _zone_health.setdefault(zone, {})
                    health["last_success"] = datetime.now(timezone.utc)
                    prev_failures = health.get("consecutive_failures", 0)
                    health["consecutive_failures"] = 0
                    health.pop("cooldown_expires_at", None)
                    outcomes = health.setdefault("recent_outcomes", _deque(maxlen=ZONE_HISTORY_WINDOW))
                    outcomes.append(True)
                    was_in_cooldown = zone in _zone_failures
                    cooldown_since = _zone_failures.pop(zone, None)

                if was_in_cooldown and prev_failures > 0:
                    cooldown_duration = (datetime.now(timezone.utc) - cooldown_since).total_seconds()
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
                        existing = instance_client.get(project=GCP_PROJECT_ID, zone=zone, instance=vm_instance_id)
                        existing_status = getattr(existing, "status", "") or ""
                        if existing_status in ("RUNNING", "STAGING", "PROVISIONING"):
                            logger.warning(
                                f"[PROVISION] {vm_instance_id} already exists in {zone} "
                                f"(status={existing_status}) — adopting rather than recreating"
                            )
                            with _zone_lock:
                                _vm_zones[vm_instance_id] = zone
                                health = _zone_health.setdefault(zone, {})
                                health["last_success"] = datetime.now(timezone.utc)
                                health["consecutive_failures"] = 0
                                health.pop("cooldown_expires_at", None)
                                outcomes = health.setdefault("recent_outcomes", _deque(maxlen=ZONE_HISTORY_WINDOW))
                                outcomes.append(True)
                                _zone_failures.pop(zone, None)
                            return f"adopted-{vm_instance_id}-in-{zone}"
                        logger.warning(
                            f"[PROVISION] {vm_instance_id} exists in {zone} but status={existing_status} — "
                            f"deleting and retrying insert"
                        )
                        del_op = instance_client.delete(project=GCP_PROJECT_ID, zone=zone, instance=vm_instance_id)
                        del_op.result(timeout=180)
                    except Exception as adopt_exc:
                        logger.warning(
                            f"[PROVISION] {vm_instance_id} 409 recovery in {zone} failed: {adopt_exc}"
                        )
                    # Fall through to the retryable-error branch so we try the next zone
                    # if adoption didn't succeed.
                    continue
                # Retryable zone/infra errors — try next zone
                retryable = (
                    "does not have enough resources" in err_str
                    or "ZONE_RESOURCE_POOL_EXHAUSTED" in err_str
                    or "QUOTA" in err_str
                    or "subnetwork" in err_str.lower()  # Subnet not found (infra not provisioned)
                    or "was not found" in err_str
                )
                if retryable:
                    # Classify failure reason for operators
                    if "QUOTA" in err_str:
                        reason = "QUOTA"
                    elif "ZONE_RESOURCE_POOL_EXHAUSTED" in err_str:
                        reason = "RESOURCE_POOL_EXHAUSTED"
                    elif "subnetwork" in err_str.lower() or "was not found" in err_str:
                        reason = "SUBNET_NOT_FOUND"
                    else:
                        reason = "RESOURCE_UNAVAILABLE"

                    with _zone_lock:
                        now_fail = datetime.now(timezone.utc)
                        entering_cooldown = zone not in _zone_failures
                        _zone_failures[zone] = now_fail
                        health = _zone_health.setdefault(zone, {})
                        old_cf = health.get("consecutive_failures", 0)
                        last_fail_time = health.get("last_failure")
                        # Debounce parallel failures: with N concurrent slots, a single zone
                        # hiccup can trigger N simultaneous failures, jumping consecutive_failures
                        # from 0→N and causing exponential cooldown escalation. If the last
                        # failure was within the base cooldown window, treat this as part of the
                        # same failure event — update the timestamp but don't increment the counter.
                        within_debounce = (
                            last_fail_time is not None
                            and (now_fail - last_fail_time).total_seconds() < ZONE_COOLDOWN_SECONDS
                        )
                        if within_debounce:
                            new_failure_count = old_cf  # Parallel failure of same event — no escalation
                        else:
                            new_failure_count = old_cf + 1
                        health["last_failure"] = now_fail
                        health["consecutive_failures"] = new_failure_count
                        health["last_failure_reason"] = reason
                        cooldown_secs = _zone_cooldown_seconds(new_failure_count)
                        health["cooldown_expires_at"] = now_fail + timedelta(seconds=cooldown_secs)
                        outcomes = health.setdefault("recent_outcomes", _deque(maxlen=ZONE_HISTORY_WINDOW))
                        outcomes.append(False)

                    if entering_cooldown:
                        logger.warning(
                            f"Zone {zone} entering cooldown: {new_failure_count} consecutive failure(s), "
                            f"reason={reason}, cooldown={ZONE_COOLDOWN_SECONDS}s"
                        )
                    logger.warning(f"Zone {zone} unavailable ({reason}) for {vm_instance_id}, trying next zone...")
                    continue
                # Non-retryable error — don't retry
                raise

        if skipped_cooldown:
            logger.warning(f"Skipped {len(skipped_cooldown)} zones in cooldown: {skipped_cooldown}")
        if not last_error and len(skipped_cooldown) == len(active_zones):
            # All ACTIVE zones in cooldown — find the one expiring soonest and wait (up to 2 min).
            # Disabled zones are excluded; they have no subnet, so even waiting wouldn't help.
            now_check = datetime.now(timezone.utc)
            soonest_zone = None
            soonest_wait = float('inf')
            for z, sn in active_zones:
                expires = _zone_health.get(z, {}).get("cooldown_expires_at")
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
                import time as _time
                _time.sleep(max(0, soonest_wait) + 1)  # Safe to block — already in thread
                # Retry the soonest zone
                try:
                    instance_config.machine_type = f"zones/{wait_zone}/machineTypes/{MACHINE_TYPE}"
                    zone_region = "-".join(wait_zone.split("-")[:-1])
                    network_interface.subnetwork = f"projects/{GCP_PROJECT_ID}/regions/{zone_region}/subnetworks/{wait_subnet}"
                    operation = instance_client.insert(project=GCP_PROJECT_ID, zone=wait_zone, instance_resource=instance_config)
                    operation.result(timeout=240)
                    with _zone_lock:
                        _vm_zones[vm_instance_id] = wait_zone
                        health = _zone_health.setdefault(wait_zone, {})
                        health["last_success"] = datetime.now(timezone.utc)
                        health["consecutive_failures"] = 0
                        health.pop("cooldown_expires_at", None)
                        outcomes = health.setdefault("recent_outcomes", _deque(maxlen=ZONE_HISTORY_WINDOW))
                        outcomes.append(True)
                        _zone_failures.pop(wait_zone, None)
                    logger.info(f"Successfully provisioned VM {vm_instance_id} in {wait_zone} (after cooldown wait)")
                    return operation.name
                except Exception:
                    logger.exception(f"Retry after cooldown wait failed for {wait_zone}")
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
            # 420s outer = 240s inner insert + 180s headroom for stale-delete,
            # zone iteration, and secret setup. Previously 360s, which routinely
            # cancelled near-complete GCP operations (surfaced as
            # OPERATION_CANCELED_BY_USER 409 from the in-flight thread).
            operation_name = await asyncio.wait_for(asyncio.to_thread(_insert_instance), timeout=420)
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
        else:
            logger.exception(f"Failed to provision VM {vm_instance_id}")
        _last_provision_error[vm_instance_id] = err_str[:300]
        if len(_last_provision_error) > _PROVISION_ERROR_MAX:
            _last_provision_error.popitem(last=False)
        await asyncio.to_thread(_delete_agent_secret, vm_instance_id)
        return False
    finally:
        await _release_provision_slot(vm_instance_id)


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
            vm_name, zone, type(e).__name__, e,
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
        instance = client.get(project=GCP_PROJECT_ID, zone=zone, instance=vm_name, timeout=10)
        return getattr(instance, "status", None) or None
    except Exception as e:
        if "was not found" in str(e) or "404" in str(e):
            # VM already gone — Spot DELETE termination action means preempted
            # instances are deleted, not just stopped. Return a synthetic
            # "DELETED" so callers can distinguish "vanished" from "API errored".
            return "DELETED"
        logger.debug(f"[COMPUTE] get_vm_status failed for vm={vm_name} zone={zone}: {e}")
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


def _list_vms_in_zone(zone: str) -> list[tuple[str, str, str]]:
    """List all agent-vm-* instances in a single zone. Returns [(name, zone, status)]."""
    from google.cloud import compute_v1
    results = []
    try:
        client = compute_v1.InstancesClient()
        request = compute_v1.ListInstancesRequest(
            project=GCP_PROJECT_ID, zone=zone, filter='name:agent-vm-'
        )
        # Per-RPC timeout so a single unresponsive zone can't freeze the whole cleanup cycle.
        results = [(instance.name, zone, instance.status) for instance in client.list(request=request, timeout=30)]
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
        zones_to_try.extend(z for z, _ in FALLBACK_ZONES if z not in zones_to_try)

        for zone in zones_to_try:
            try:
                instance = instance_client.get(project=GCP_PROJECT_ID, zone=zone, instance=vm_instance_id)
                # Serial capture only makes sense while the agent was running — for already
                # TERMINATED/STOPPING VMs the buffer is just shutdown output, and the 10s call
                # eats into the delete budget for no forensic gain.
                status = getattr(instance, "status", "") or ""
                if status in ("TERMINATED", "STOPPING"):
                    serial_tail = None
                else:
                    serial_tail = _capture_serial_for_vm(vm_instance_id, zone)
                operation = instance_client.delete(project=GCP_PROJECT_ID, zone=zone, instance=vm_instance_id)
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
                asyncio.to_thread(_find_capture_and_delete),
                timeout=240
            )

        if serial_tail:
            logger.info(f"[VM-SERIAL] {vm_instance_id} last output:\n{serial_tail}")

        # Best-effort: clean up any leftover credentials secret
        async with _gcp_ops_semaphore:
            await asyncio.to_thread(_delete_agent_secret, vm_instance_id)
        return True
    except asyncio.TimeoutError:
        logger.warning(f"Timeout deleting VM {vm_instance_id} (>240s)")
        return False
    except Exception:
        logger.warning(f"Failed to terminate VM {vm_instance_id}", exc_info=True)
        return False


# ─── Bulk Orphan Cleanup ──────────────────────────────────────────────────

class CleanupResult:
    """Structured result from orphan cleanup."""
    __slots__ = ("deleted", "failed", "orphan_count", "total_gcp_vms")

    def __init__(self):
        self.total_gcp_vms: int = 0
        self.orphan_count: int = 0
        self.deleted: list[str] = []
        self.failed: list[str] = []


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
    unique_zones = list({z for z, _ in FALLBACK_ZONES})
    logger.info(f"[GCP CLEANUP] Cycle starting — scanning {len(unique_zones)} zones, DB active: {len(active_vm_ids)}")

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
    orphans = [(name, zone, status) for name, zone, status in all_vms if name not in active_set]
    result.orphan_count = len(orphans)

    logger.info(
        f"[GCP CLEANUP] GCP inventory: {result.total_gcp_vms} VMs, "
        f"DB active: {len(active_vm_ids)}, orphans: {result.orphan_count}"
    )

    if not orphans:
        return result

    # Phase 3: Parallel deletion (no blocking serial capture).
    # _format_orphan_status tags TERMINATED+SPOT VMs as "(likely spot
    # preemption)" so operators distinguish capacity-driven kills from
    # agent crashes when scanning logs.
    async def _delete_orphan(name: str, zone: str, status: str):
        logger.warning(f"[GCP CLEANUP] Deleting orphan {name} ({zone}, {_format_orphan_status(status)})")
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
                    logger.exception(f"[GCP CLEANUP] Failed to clean up secret for orphan {name}")
            else:
                result.failed.append(name)
        except asyncio.TimeoutError:
            result.failed.append(name)
            logger.exception(f"[GCP CLEANUP] Timeout deleting orphan {name}")
        except Exception:
            result.failed.append(name)
            logger.exception(f"[GCP CLEANUP] Exception deleting orphan {name}")

    delete_tasks = [_delete_orphan(name, zone, status) for name, zone, status in orphans]
    await asyncio.gather(*delete_tasks, return_exceptions=True)

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
            success_rate = (sum(outcomes) / provision_count) if provision_count else None

            result[zone] = {
                "status": status,
                "last_success": data["last_success"].isoformat() if data.get("last_success") else None,
                "last_failure": data["last_failure"].isoformat() if data.get("last_failure") else None,
                "last_failure_reason": data.get("last_failure_reason"),
                "consecutive_failures": consecutive,
                "cooldown_active": cooldown_active,
                "cooldown_expires_at": cooldown_expires.isoformat() if cooldown_active and cooldown_expires else None,
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
        z: d["success_rate"]
        for z, d in health.items()
        if d["success_rate"] is not None
    }
    active_total = len(FALLBACK_ZONES) - len(disabled)
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
    }


def any_zone_available() -> bool:
    """Return True if at least one ACTIVE zone is not in cooldown.

    Disabled zones are excluded — they have no subnet provisioned and cannot
    accept VMs even if not in cooldown.
    """
    now = datetime.now(timezone.utc)
    with _zone_lock:
        for zone, _ in _active_fallback_zones():
            health = _zone_health.get(zone)
            if health is None:
                return True  # Unknown = available
            cooldown = health.get('cooldown_expires_at')
            if cooldown is None or now >= cooldown:
                return True
    return False


def record_zone_failure(zone: str, reason: str = "HEARTBEAT_STARVATION") -> None:
    """Record a zone failure from outside the provisioning path (e.g. reaper).

    Called when a VM was provisioned in a zone but the agent died without
    heartbeating, indicating the zone may have issues even though the GCP
    API call succeeded.
    """
    with _zone_lock:
        now_fail = datetime.now(timezone.utc)
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
        if within_debounce:
            new_failure_count = old_cf
        else:
            new_failure_count = old_cf + 1
        health["last_failure"] = now_fail
        health["consecutive_failures"] = new_failure_count
        health["last_failure_reason"] = reason
        cooldown_secs = _zone_cooldown_seconds(new_failure_count)
        health["cooldown_expires_at"] = now_fail + timedelta(seconds=cooldown_secs)
        outcomes = health.setdefault("recent_outcomes", _deque(maxlen=ZONE_HISTORY_WINDOW))
        outcomes.append(False)

    if entering_cooldown:
        logger.warning(
            f"Zone {zone} entering cooldown (from reaper): {new_failure_count} consecutive failure(s), "
            f"reason={reason}, cooldown={cooldown_secs}s"
        )


def soonest_zone_recovery() -> datetime | None:
    """Return the earliest ACTIVE zone cooldown expiry, or None if a zone is available.

    Disabled zones are excluded — they have no subnet so a "recovered" cooldown
    in a disabled region wouldn't unlock spawning anyway.
    """
    now = datetime.now(timezone.utc)
    soonest = None
    with _zone_lock:
        for zone, _ in _active_fallback_zones():
            health = _zone_health.get(zone)
            if health is None:
                return None  # Unknown = available now
            cooldown = health.get('cooldown_expires_at')
            if cooldown is None or now >= cooldown:
                return None  # Available now
            if soonest is None or cooldown < soonest:
                soonest = cooldown
    return soonest
