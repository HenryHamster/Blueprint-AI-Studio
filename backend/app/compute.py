import os
import re
import uuid
import time
import logging
import asyncio
import subprocess
import signal
import threading
from collections import OrderedDict as _OrderedDict
from datetime import datetime, timezone, timedelta
from typing import Optional

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

# Track which zone each VM was created in (for delete/cleanup)
_vm_zones: dict[str, str] = {}

# Semaphores to cap concurrent GCP API calls — prevents thread pool starvation
# and stays well under GCP's 20 QPS rate limit.
# Separate limits for slow insert (up to 300s) vs fast ops (list/delete/get, ~10s)
# so long-running provisions don't starve orphan cleanup.
# Local semaphore still used as secondary guard even with Redis (prevents thread starvation).
_gcp_provision_semaphore = asyncio.Semaphore(6)  # insert/create — long-running (up to 300s)
_gcp_ops_semaphore = asyncio.Semaphore(8)         # list/delete/get — fast ops (~10s)

# Distributed provision semaphore via Redis sorted set (falls back to local-only)
_PROVISION_REDIS_KEY = "blueprint:active-provisions"
_MAX_GLOBAL_PROVISIONS = 6
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
            pass  # Best-effort — TTL will reclaim

# Zone cooldown: skip zones that recently failed (avoid wasting time retrying exhausted zones)
_zone_failures: dict[str, datetime] = {}
ZONE_COOLDOWN_SECONDS = 300  # 5 minutes — base for exponential backoff


def _zone_cooldown_seconds(consecutive_failures: int) -> int:
    """Exponential backoff: 5min → 10min → 20min → ... capped at 2 hours."""
    return min(ZONE_COOLDOWN_SECONDS * (2 ** consecutive_failures), 7200)

# Zone health tracking: populated by provisioning attempts
_zone_health: dict[str, dict] = {}
# Structure: { "us-central1-f": {"last_success": datetime, "last_failure": datetime, "consecutive_failures": 0, "last_failure_reason": str} }

# Thread lock for zone state dicts (modified from asyncio.to_thread workers, read from event loop)
_zone_lock = threading.RLock()
MACHINE_TYPE = os.getenv("MACHINE_TYPE", "e2-standard-2")

# Networking Configuration
VPC_NETWORK = os.getenv("VPC_NETWORK", "agent-sandbox-vpc")
VPC_SUBNET = os.getenv("VPC_SUBNET", "agent-sandbox-subnet")

# The base image for our agent
AGENT_IMAGE_FAMILY = os.getenv("AGENT_IMAGE_FAMILY", f"projects/{GCP_PROJECT_ID}/global/images/family/blueprint-agent")

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
        logger.error(f"[STARTUP] Image family validation failed: {e}")
        raise RuntimeError(f"AGENT_IMAGE_FAMILY '{AGENT_IMAGE_FAMILY}' not found: {e}")

_image_family_valid: Optional[bool] = None  # None = not yet checked, True/False = result
_image_family_last_check: float = 0.0
_IMAGE_FAMILY_RECHECK_INTERVAL: float = 300.0  # Retry validation every 5 min if failed

if not LOCAL_MODE and os.getenv("K_REVISION"):  # K_REVISION set by Cloud Run — skip in dev/test
    _validate_env_config()
    try:
        _validate_image_family()
        _image_family_valid = True
        _image_family_last_check = time.time()
    except Exception as e:
        _image_family_valid = False
        _image_family_last_check = time.time()
        logger.error(f"[STARTUP] Image family validation FAILED — will retry in {_IMAGE_FAMILY_RECHECK_INTERVAL}s: {e}")

from app.auth import create_agent_token  # must be after env vars are loaded above

ARTIFACT_BUCKET_NAME = os.getenv("ARTIFACT_BUCKET_NAME", "blueprint-agent-artifacts")

# --- Payload URI Validation ---
_PAYLOAD_URI_RE = re.compile(r"^gs://[a-zA-Z0-9._-]+/[a-zA-Z0-9._/-]+$")


def validate_payload_uri(payload_uri: str) -> None:
    """Raise ValueError if payload_uri is not a safe GCS URI."""
    if not _PAYLOAD_URI_RE.match(payload_uri):
        raise ValueError(
            f"Invalid payload_uri: must match gs://<bucket>/<object> with "
            f"alphanumeric, dots, dashes, underscores, and slashes only. Got: {payload_uri!r}"
        )


# --- Local Mode: Track subprocesses ---
_local_processes: dict[str, subprocess.Popen] = {}


async def provision_agent_vm(vm_instance_id: str, task_id: uuid.UUID, payload_uri: str | None = None,
                              oauth_token: str | None = None, claude_config_json: str | None = None):
    """
    Provisions an agent. In LOCAL_MODE, spawns a subprocess.
    In production, creates a GCP Compute Engine VM.
    oauth_token: Long-lived OAuth token (sk-ant-oat01-...) for Claude Code CLI.
    claude_config_json: Content of ~/.claude.json for onboarding bypass.
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
            except Exception as e:
                _image_family_last_check = now
                logger.error(f"[COMPUTE] Image family re-validation still failing: {e}")
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
        return await _provision_local(vm_instance_id, task_id, payload_uri, oauth_token)
    return await _provision_gcp(vm_instance_id, task_id, payload_uri, oauth_token, claude_config_json)


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


# ─── Local Mode Implementation ──────────────────────────────────────────────

async def _provision_local(vm_instance_id: str, task_id: uuid.UUID, payload_uri: str | None = None, oauth_token: str | None = None):
    logger.info(f"[LOCAL] Spawning agent subprocess: {vm_instance_id} (Task: {task_id})")

    agent_jwt = create_agent_token(str(task_id), vm_instance_id)

    env = {
        **os.environ,
        "VM_INSTANCE_ID": vm_instance_id,
        "ORCHESTRATOR_URL": ORCHESTRATOR_URL,
        "AGENT_SECRET_TOKEN": agent_jwt,
        "PAYLOAD_URI": payload_uri or "",
    }
    # OAuth token (primary) — passed as env var, Claude Code CLI reads it directly
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    # API key (fallback) — only if no OAuth token
    elif ANTHROPIC_API_KEY:
        env["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY

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
    except Exception as e:
        logger.error(f"[LOCAL] Failed to spawn agent: {e}")
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
        except Exception as e:
            logger.error(f"[LOCAL] Failed to kill subprocess: {e}")
            return False
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

    # Grant the agent service account secretAccessor on this specific secret only
    policy = client.get_iam_policy(request={"resource": secret.name})
    binding = policy_pb2.Binding(
        role="roles/secretmanager.secretAccessor",
        members=[f"serviceAccount:{AGENT_SERVICE_ACCOUNT_EMAIL}"],
    )
    policy.bindings.append(binding)
    client.set_iam_policy(request={"resource": secret.name, "policy": policy})

    logger.info(f"Stored agent credentials in Secret Manager: {secret_id}")
    return version.name


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
                         oauth_token: str | None = None, claude_config_json: str | None = None):
    from google.cloud import compute_v1

    logger.info(f"Starting provisioning for VM: {vm_instance_id} (Task: {task_id})")

    agent_jwt = create_agent_token(str(task_id), vm_instance_id)

    # Build the env file content that will be stored in Secret Manager.
    # No credentials are embedded in the startup script metadata — all secrets
    # (including the agent JWT) are retrieved at runtime from Secret Manager.
    secret_env_lines = [
        f"AGENT_SECRET_TOKEN={agent_jwt}",
        f"CLAUDE_CODE_OAUTH_TOKEN={oauth_token}" if oauth_token else None,
        f"ANTHROPIC_API_KEY={ANTHROPIC_API_KEY}" if (not oauth_token and ANTHROPIC_API_KEY) else None,
    ]
    if claude_config_json:
        # Store claude config as a separate secret
        import base64 as _b64
        # We'll embed it as base64 in the credentials secret so one Secret Manager
        # access retrieves everything the VM needs.
        config_b64 = _b64.b64encode(claude_config_json.encode()).decode()
        secret_env_lines.append(f"CLAUDE_CONFIG_B64={config_b64}")

    secret_env_content = "\n".join(line for line in secret_env_lines if line)

    # payload_uri is validated upstream and safe to embed in the startup script
    import base64
    payload_uri_b64 = base64.b64encode(payload_uri.encode()).decode() if payload_uri else ""

    # Store credentials in Secret Manager and get the version resource name
    secret_version_name = await asyncio.to_thread(
        _store_agent_secret, vm_instance_id, secret_env_content
    )

    startup_script = f"""#!/bin/bash
    set -euo pipefail
    trap 'echo "FATAL: Startup script failed at line $LINENO"; poweroff' ERR
    echo "Starting Agent VM Initialization..."

    # 1. Self-Destruct Mechanism (Defense in Depth)
    # Graceful: stop the service first (SIGTERM), wait 30s for cleanup, then poweroff
    TTL_SECONDS=3600
    (
      sleep $TTL_SECONDS
      echo "TTL reached ($TTL_SECONDS s). Gracefully stopping agent service..."
      systemctl stop blueprint-agent 2>/dev/null || true
      sleep 30
      echo "Force terminating VM..."
      poweroff
    ) &
    TTL_PID=$!

    # 2. Retrieve credentials from Secret Manager (never stored in instance metadata)
    SECRET_VERSION="{secret_version_name}"
    echo "Fetching agent credentials from Secret Manager..."
    CREDS="$(gcloud secrets versions access "$SECRET_VERSION")"

    # 3. Write static env vars first, then append secrets from Secret Manager
    cat <<'STATICENV' > /etc/agent.env
VM_INSTANCE_ID="{vm_instance_id}"
ORCHESTRATOR_URL="{ORCHESTRATOR_URL}"
STATICENV

    # Append the credential lines retrieved from Secret Manager
    echo "$CREDS" >> /etc/agent.env

    # Add PAYLOAD_URI if present (validated safe GCS URI — no secrets)
    if [ -n "{payload_uri_b64}" ]; then
        echo "PAYLOAD_URI=\\"$(echo '{payload_uri_b64}' | base64 -d)\\"" >> /etc/agent.env
    fi

    # 4. Claude Code onboarding bypass — extract from env if present
    CLAUDE_CONFIG_B64="$(grep '^CLAUDE_CONFIG_B64=' /etc/agent.env | cut -d= -f2- || true)"
    if [ -n "$CLAUDE_CONFIG_B64" ]; then
        echo "$CLAUDE_CONFIG_B64" | base64 -d > /home/agent/.claude.json
        chown agent:agent /home/agent/.claude.json
        # Remove config b64 from env file (not needed at runtime)
        sed -i '/^CLAUDE_CONFIG_B64=/d' /etc/agent.env
        echo "Claude Code config installed."
    fi

    # 5. Download payload if provided
    if [ -n "{payload_uri_b64}" ]; then
        PAYLOAD_URI_DECODED="$(echo '{payload_uri_b64}' | base64 -d)"
        echo "Downloading payload from $PAYLOAD_URI_DECODED..."
        gsutil cp "$PAYLOAD_URI_DECODED" /etc/agent_payload.json
    fi

    # 6. Pull latest agent source from GCS (hotfix: deploys with orchestrator, no Packer rebuild needed)
    echo "Pulling latest agent source from GCS..."
    HOTFIX_TMP="/tmp/agent-hotfix-index.ts"
    gsutil -q cp gs://{ARTIFACT_BUCKET_NAME}/agent-hotfix/index.ts "$HOTFIX_TMP" 2>/dev/null && {{
        # Validate hotfix before replacing baked-in source
        cp /opt/agent/src/index.ts /opt/agent/src/index.ts.bak
        cp "$HOTFIX_TMP" /opt/agent/src/index.ts
        chown agent:agent /opt/agent/src/index.ts
        if su agent -c "cd /opt/agent && npx tsx --no-warnings -e 'process.exit(0)'" 2>/dev/null; then
            echo "Agent source updated from GCS hotfix (validated)."
        else
            echo "GCS hotfix failed validation — reverting to baked-in source."
            mv /opt/agent/src/index.ts.bak /opt/agent/src/index.ts
        fi
        rm -f /opt/agent/src/index.ts.bak "$HOTFIX_TMP"
    }} || echo "No GCS hotfix found, using baked-in agent source."

    # 7. Start the systemd service
    echo "Starting blueprint-agent service..."
    systemctl daemon-reload
    timeout 60 systemctl restart blueprint-agent || {{
        echo "Service start failed"
        AGENT_TOKEN="$(grep '^AGENT_SECRET_TOKEN=' /etc/agent.env | cut -d= -f2-)"
        ORCH_URL="$(grep '^ORCHESTRATOR_URL=' /etc/agent.env | cut -d= -f2-)"
        SERVICE_LOG="$(journalctl -u blueprint-agent --no-pager -n 30 2>/dev/null | tr '"' "'" | tr '\\n' ' ' || echo 'no journal')"
        curl -sf -X POST "$ORCH_URL/api/agent/heartbeat" \
          -H "Content-Type: application/json" \
          -H "X-Agent-Token: $AGENT_TOKEN" \
          -d '{{"task_id": "{task_id}", "status_message": "PROVISION_HEALTH_FAILED: service start failed. Journal: '"$SERVICE_LOG"'"}}' \
          --connect-timeout 10 --max-time 15 || true
        poweroff
    }}

    # 7b. Notify orchestrator that startup completed — early health signal
    # Reads AGENT_SECRET_TOKEN from env file (written from Secret Manager above)
    AGENT_TOKEN="$(grep '^AGENT_SECRET_TOKEN=' /etc/agent.env | cut -d= -f2-)"
    ORCH_URL="$(grep '^ORCHESTRATOR_URL=' /etc/agent.env | cut -d= -f2-)"
    curl -sf -X POST "$ORCH_URL/api/agent/heartbeat" \
      -H "Content-Type: application/json" \
      -H "X-Agent-Token: $AGENT_TOKEN" \
      -d '{{"task_id": "{task_id}", "status_message": "Startup script completed, agent service started"}}' \
      --connect-timeout 10 --max-time 15 && echo "PROVISION_HEALTH_OK" || echo "PROVISION_HEALTH_FAILED (agent will self-report via heartbeat)"

    # Wait for the service to finish (with TTL guard)
    POLL_START=$(date +%s)
    while systemctl is-active --quiet blueprint-agent; do
      sleep 5
      ELAPSED=$(( $(date +%s) - POLL_START ))
      if [ $ELAPSED -gt $TTL_SECONDS ]; then
        echo "Service exceeded TTL, stopping..."
        systemctl stop blueprint-agent 2>/dev/null || true
        break
      fi
    done

    # 7. Cancel TTL watchdog (service finished early) and self-terminate
    kill $TTL_PID 2>/dev/null || true
    echo "Agent finished. Terminating VM..."
    poweroff
    """

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

    _cancel_event = threading.Event()

    def _insert_instance():
        instance_client = compute_v1.InstancesClient()
        last_error = None
        now = datetime.now(timezone.utc)
        skipped_cooldown = []

        for zone, subnet_name in FALLBACK_ZONES:
            if _cancel_event.is_set():
                logger.info(f"[PROVISION] {vm_instance_id} cancelled — skipping remaining zones")
                return None
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
                operation.result(timeout=300)

                with _zone_lock:
                    _vm_zones[vm_instance_id] = zone
                    health = _zone_health.setdefault(zone, {})
                    health["last_success"] = datetime.now(timezone.utc)
                    prev_failures = health.get("consecutive_failures", 0)
                    health["consecutive_failures"] = 0
                    health.pop("cooldown_expires_at", None)
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
                        health["last_failure"] = now_fail
                        old_cf = health.get("consecutive_failures", 0)
                        new_failure_count = old_cf + 1
                        health["consecutive_failures"] = new_failure_count
                        health["last_failure_reason"] = reason
                        cooldown_secs = _zone_cooldown_seconds(old_cf)
                        health["cooldown_expires_at"] = now_fail + timedelta(seconds=cooldown_secs)

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
        if not last_error and len(skipped_cooldown) == len(FALLBACK_ZONES):
            # All zones in cooldown — find the one expiring soonest and wait (up to 2 min)
            now_check = datetime.now(timezone.utc)
            soonest_zone = None
            soonest_wait = float('inf')
            for z, sn in FALLBACK_ZONES:
                expires = _zone_health.get(z, {}).get("cooldown_expires_at")
                if expires:
                    remaining = (expires - now_check).total_seconds()
                    if remaining < soonest_wait:
                        soonest_wait = remaining
                        soonest_zone = (z, sn)
            if soonest_zone and soonest_wait <= 120:
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
                    operation.result(timeout=300)
                    with _zone_lock:
                        _vm_zones[vm_instance_id] = wait_zone
                        health = _zone_health.setdefault(wait_zone, {})
                        health["last_success"] = datetime.now(timezone.utc)
                        health["consecutive_failures"] = 0
                        health.pop("cooldown_expires_at", None)
                        _zone_failures.pop(wait_zone, None)
                    logger.info(f"Successfully provisioned VM {vm_instance_id} in {wait_zone} (after cooldown wait)")
                    return operation.name
                except Exception as retry_err:
                    logger.error(f"Retry after cooldown wait failed for {wait_zone}: {retry_err}")
                    raise
            else:
                logger.critical(
                    f"ALL {len(FALLBACK_ZONES)} zones in cooldown, soonest expires in {soonest_wait:.0f}s (>120s cap) — "
                    f"failing this provision attempt"
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
            operation_name = await asyncio.wait_for(asyncio.to_thread(_insert_instance), timeout=360)
        logger.info(f"GCP Operation completed: {operation_name}")
        return True
    except (asyncio.TimeoutError, asyncio.CancelledError) as e:
        _cancel_event.set()  # Signal thread to stop trying more zones
        logger.error(f"Failed to provision VM {vm_instance_id}: {e}")
        _last_provision_error[vm_instance_id] = str(e)[:300]
        if len(_last_provision_error) > _PROVISION_ERROR_MAX:
            _last_provision_error.popitem(last=False)
        await asyncio.to_thread(_delete_agent_secret, vm_instance_id)
        return False
    except Exception as e:
        _cancel_event.set()
        logger.error(f"Failed to provision VM {vm_instance_id}: {e}")
        _last_provision_error[vm_instance_id] = str(e)[:300]
        if len(_last_provision_error) > _PROVISION_ERROR_MAX:
            _last_provision_error.popitem(last=False)
        await asyncio.to_thread(_delete_agent_secret, vm_instance_id)
        return False
    finally:
        await _release_provision_slot(vm_instance_id)


# ─── Shared VM Helpers (used by both delete and cleanup paths) ────────────

def _capture_serial_for_vm(vm_name: str, zone: str) -> str | None:
    """Best-effort serial capture. Returns tail or None."""
    from google.cloud import compute_v1
    try:
        client = compute_v1.InstancesClient()
        serial = client.get_serial_port_output(
            project=GCP_PROJECT_ID, zone=zone, instance=vm_name, timeout=10
        )
        return (serial.contents or "")[-2000:]
    except Exception:
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
        logger.error(f"[VM DELETE] Failed to delete {vm_name} in {zone}: {e}")
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
        results = [(instance.name, zone, instance.status) for instance in client.list(request=request)]
    except Exception as e:
        logger.error(f"[GCP CLEANUP] Failed to list VMs in zone {zone}: {e}")
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
                instance_client.get(project=GCP_PROJECT_ID, zone=zone, instance=vm_instance_id)
                # Capture serial BEFORE deletion (VM still exists)
                serial_tail = _capture_serial_for_vm(vm_instance_id, zone)
                # Now delete
                operation = instance_client.delete(project=GCP_PROJECT_ID, zone=zone, instance=vm_instance_id)
                operation.result(timeout=300)
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
            deleted_zone, serial_tail = await asyncio.wait_for(
                asyncio.to_thread(_find_capture_and_delete),
                timeout=60
            )

        if serial_tail:
            logger.info(f"[VM-SERIAL] {vm_instance_id} last output:\n{serial_tail}")

        # Best-effort: clean up any leftover credentials secret
        async with _gcp_ops_semaphore:
            await asyncio.to_thread(_delete_agent_secret, vm_instance_id)
        return True
    except asyncio.TimeoutError:
        logger.error(f"Timeout deleting VM {vm_instance_id} (>60s)")
        return False
    except Exception as e:
        logger.error(f"Failed to terminate VM {vm_instance_id}: {e}")
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

    # Phase 3: Parallel deletion (no blocking serial capture)
    async def _delete_orphan(name: str, zone: str, status: str):
        logger.warning(f"[GCP CLEANUP] Deleting orphan {name} ({zone}, {status})")
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
                    pass
            else:
                result.failed.append(name)
        except asyncio.TimeoutError:
            result.failed.append(name)
            logger.error(f"[GCP CLEANUP] Timeout deleting orphan {name}")
        except Exception as e:
            result.failed.append(name)
            logger.error(f"[GCP CLEANUP] Exception deleting orphan {name}: {e}")

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
            if consecutive == 0:
                status = "healthy"
            elif cooldown_active:
                status = "exhausted"
            else:
                status = "recovering"

            result[zone] = {
                "status": status,
                "last_success": data["last_success"].isoformat() if data.get("last_success") else None,
                "last_failure": data["last_failure"].isoformat() if data.get("last_failure") else None,
                "last_failure_reason": data.get("last_failure_reason"),
                "consecutive_failures": consecutive,
                "cooldown_active": cooldown_active,
            }
    return result


def get_zone_health_summary() -> dict:
    """Return a compact summary for health endpoints."""
    health = get_zone_health()
    exhausted = [z for z, d in health.items() if d["status"] == "exhausted"]
    return {
        "total_zones": len(FALLBACK_ZONES),
        "available": len(FALLBACK_ZONES) - len(exhausted),
        "exhausted": exhausted,
    }
