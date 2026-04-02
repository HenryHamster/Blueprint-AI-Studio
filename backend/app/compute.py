import os
import uuid
import logging
import asyncio
import subprocess
import signal
import threading
from datetime import datetime, timezone

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

# Zone cooldown: skip zones that recently failed (avoid wasting time retrying exhausted zones)
_zone_failures: dict[str, datetime] = {}
ZONE_COOLDOWN_SECONDS = 300  # 5 minutes

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

from app.auth import create_agent_token  # noqa: E402 — must be after env vars

ARTIFACT_BUCKET_NAME = os.getenv("ARTIFACT_BUCKET_NAME", "blueprint-agent-artifacts")

# --- Local Mode: Track subprocesses ---
_local_processes: dict[str, subprocess.Popen] = {}


async def provision_agent_vm(vm_instance_id: str, task_id: uuid.UUID, payload_uri: str = None,
                              oauth_token: str = None, claude_config_json: str = None):
    """
    Provisions an agent. In LOCAL_MODE, spawns a subprocess.
    In production, creates a GCP Compute Engine VM.
    oauth_token: Long-lived OAuth token (sk-ant-oat01-...) for Claude Code CLI.
    claude_config_json: Content of ~/.claude.json for onboarding bypass.
    """
    if LOCAL_MODE:
        return await _provision_local(vm_instance_id, task_id, payload_uri, oauth_token)
    return await _provision_gcp(vm_instance_id, task_id, payload_uri, oauth_token, claude_config_json)


_last_provision_error: dict[str, str] = {}

def get_provision_error(vm_instance_id: str) -> str:
    """Get and clear the last provisioning error for a VM."""
    return _last_provision_error.pop(vm_instance_id, "")


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

async def _provision_local(vm_instance_id: str, task_id: uuid.UUID, payload_uri: str = None, oauth_token: str = None):
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

        asyncio.create_task(_monitor())
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
    orphaned = [vid for vid in list(_local_processes.keys()) if vid not in active_vm_ids]
    for vid in orphaned:
        logger.warning(f"[LOCAL] Cleaning up orphaned agent: {vid}")
        await _delete_local(vid)
    return len(orphaned)


# ─── GCP Mode Implementation ────────────────────────────────────────────────

async def _provision_gcp(vm_instance_id: str, task_id: uuid.UUID, payload_uri: str = None,
                         oauth_token: str = None, claude_config_json: str = None):
    from google.cloud import compute_v1

    logger.info(f"Starting provisioning for VM: {vm_instance_id} (Task: {task_id})")

    agent_jwt = create_agent_token(str(task_id), vm_instance_id)

    # Base64-encode all secrets to prevent heredoc/shell injection
    import base64
    config_b64 = base64.b64encode((claude_config_json or "").encode()).decode() if claude_config_json else ""
    oauth_b64 = base64.b64encode(oauth_token.encode()).decode() if oauth_token else ""
    apikey_b64 = base64.b64encode(ANTHROPIC_API_KEY.encode()).decode() if (not oauth_token and ANTHROPIC_API_KEY) else ""

    startup_script = f"""#!/bin/bash
    echo "Starting Agent VM Initialization..."

    # 1. Self-Destruct Mechanism (Defense in Depth)
    (sleep 3600 && echo "Maximum TTL reached. Self-terminating..." && poweroff) &

    # 2. Export environment variables needed by the agent
    # Static values are safe in heredoc; secrets are base64-decoded separately
    cat <<'EOF' > /etc/agent.env
VM_INSTANCE_ID="{vm_instance_id}"
ORCHESTRATOR_URL="{ORCHESTRATOR_URL}"
AGENT_SECRET_TOKEN="{agent_jwt}"
PAYLOAD_URI="{payload_uri or ''}"
EOF

    # Decode secrets from base64 and append to env file (avoids shell injection via heredoc)
    if [ -n "{oauth_b64}" ]; then
        echo "CLAUDE_CODE_OAUTH_TOKEN=\\"$(echo '{oauth_b64}' | base64 -d)\\"" >> /etc/agent.env
    fi
    if [ -n "{apikey_b64}" ]; then
        echo "ANTHROPIC_API_KEY=\\"$(echo '{apikey_b64}' | base64 -d)\\"" >> /etc/agent.env
    fi

    # 3. Claude Code onboarding bypass — write ~/.claude.json from base64-encoded blob
    if [ -n "{config_b64}" ]; then
        echo "{config_b64}" | base64 -d > /home/agent/.claude.json
        chown agent:agent /home/agent/.claude.json
        echo "Claude Code config installed."
    fi

    # 4. Download payload if provided
    if [ -n "{payload_uri or ''}" ]; then
        echo "Downloading payload from {payload_uri}..."
        gsutil cp "{payload_uri}" /etc/agent_payload.json
    fi

    # 5. Start the systemd service
    echo "Starting blueprint-agent service..."
    systemctl daemon-reload
    systemctl restart blueprint-agent

    # Wait for the service to finish
    while systemctl is-active --quiet blueprint-agent; do
      sleep 5
    done

    # 6. Graceful self-termination
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
    metadata.items = [metadata_item]
    instance_config.metadata = metadata

    service_account = compute_v1.ServiceAccount()
    service_account.email = AGENT_SERVICE_ACCOUNT_EMAIL
    service_account.scopes = [
        "https://www.googleapis.com/auth/devstorage.read_only",
        "https://www.googleapis.com/auth/logging.write"
    ]
    instance_config.service_accounts = [service_account]

    def _insert_instance():
        instance_client = compute_v1.InstancesClient()
        last_error = None
        now = datetime.now(timezone.utc)
        skipped_cooldown = []

        for zone, subnet_name in FALLBACK_ZONES:
            # Skip zones in cooldown (recently failed)
            last_fail = _zone_failures.get(zone)
            if last_fail and (now - last_fail).total_seconds() < ZONE_COOLDOWN_SECONDS:
                skipped_cooldown.append(zone)
                continue

            try:
                # Update zone-dependent config for this attempt
                instance_config.machine_type = f"zones/{zone}/machineTypes/{MACHINE_TYPE}"
                zone_region = "-".join(zone.split("-")[:-1])
                network_interface.subnetwork = f"projects/{GCP_PROJECT_ID}/regions/{zone_region}/subnetworks/{subnet_name}"

                # Delete any existing VM with this name first (stale from retries)
                try:
                    instance_client.get(project=GCP_PROJECT_ID, zone=zone, instance=vm_instance_id)
                    logger.info(f"Deleting stale VM {vm_instance_id} in {zone} before re-provisioning")
                    del_op = instance_client.delete(project=GCP_PROJECT_ID, zone=zone, instance=vm_instance_id)
                    del_op.result()
                except Exception as e:
                    if "was not found" not in str(e) and "404" not in str(e):
                        logger.warning(f"Failed to clean stale VM {vm_instance_id} in {zone}: {e}")

                operation = instance_client.insert(
                    project=GCP_PROJECT_ID,
                    zone=zone,
                    instance_resource=instance_config
                )
                operation.result()

                with _zone_lock:
                    _vm_zones[vm_instance_id] = zone
                    health = _zone_health.setdefault(zone, {})
                    health["last_success"] = datetime.now(timezone.utc)
                    prev_failures = health.get("consecutive_failures", 0)
                    health["consecutive_failures"] = 0
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
                        entering_cooldown = zone not in _zone_failures
                        _zone_failures[zone] = datetime.now(timezone.utc)
                        health = _zone_health.setdefault(zone, {})
                        health["last_failure"] = datetime.now(timezone.utc)
                        new_failure_count = health.get("consecutive_failures", 0) + 1
                        health["consecutive_failures"] = new_failure_count
                        health["last_failure_reason"] = reason

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
            logger.critical(
                f"ALL {len(FALLBACK_ZONES)} zones are in cooldown simultaneously — "
                f"no capacity available, retry after {ZONE_COOLDOWN_SECONDS}s. "
                f"Zones: {[z for z, _ in FALLBACK_ZONES]}"
            )
            raise Exception(f"All {len(FALLBACK_ZONES)} zones in cooldown — retry after {ZONE_COOLDOWN_SECONDS}s")
        raise last_error or Exception("All zones exhausted")

    try:
        operation_name = await asyncio.to_thread(_insert_instance)
        logger.info(f"GCP Operation completed: {operation_name}")
        return True
    except Exception as e:
        logger.error(f"Failed to provision VM {vm_instance_id}: {e}")
        _last_provision_error[vm_instance_id] = str(e)[:300]
        return False


async def _delete_gcp(vm_instance_id: str):
    from google.cloud import compute_v1

    logger.info(f"Issuing external kill command for VM: {vm_instance_id}")

    def _delete_instance():
        instance_client = compute_v1.InstancesClient()
        # Check known zone first, then try all fallback zones
        zones_to_try = []
        with _zone_lock:
            known_zone = _vm_zones.pop(vm_instance_id, None)
        if known_zone:
            zones_to_try.append(known_zone)
        zones_to_try.extend(z for z, _ in FALLBACK_ZONES if z not in zones_to_try)

        for zone in zones_to_try:
            try:
                instance_client.get(project=GCP_PROJECT_ID, zone=zone, instance=vm_instance_id)
                operation = instance_client.delete(project=GCP_PROJECT_ID, zone=zone, instance=vm_instance_id)
                operation.result()
                logger.info(f"Successfully terminated VM {vm_instance_id} in {zone}")
                return operation.name
            except Exception as e:
                if "was not found" in str(e) or "404" in str(e):
                    continue
                raise
        logger.warning(f"VM {vm_instance_id} not found in any zone")
        return "not_found"

    try:
        await asyncio.to_thread(_delete_instance)
        return True
    except Exception as e:
        logger.error(f"Failed to terminate VM {vm_instance_id}: {e}")
        return False


async def _cleanup_gcp(active_vm_ids: list[str]):
    from google.cloud import compute_v1

    logger.info("Scanning for orphaned VMs in GCP...")

    def _list_and_delete():
        instance_client = compute_v1.InstancesClient()
        orphaned_count = 0

        for zone, _ in FALLBACK_ZONES:
            try:
                request = compute_v1.ListInstancesRequest(
                    project=GCP_PROJECT_ID,
                    zone=zone,
                    filter='name:agent-vm-'
                )
                for instance in instance_client.list(request=request):
                    if instance.name not in active_vm_ids:
                        logger.warning(f"Found orphaned VM in GCP: {instance.name} ({zone}). Terminating...")
                        try:
                            operation = instance_client.delete(
                                project=GCP_PROJECT_ID,
                                zone=zone,
                                instance=instance.name
                            )
                            operation.result()
                            orphaned_count += 1
                        except Exception as e:
                            logger.error(f"Failed to delete orphaned VM {instance.name}: {e}")
            except Exception as e:
                logger.error(f"Failed to list VMs in zone {zone}: {e}")

        return orphaned_count

    try:
        count = await asyncio.to_thread(_list_and_delete)
        if count > 0:
            logger.info(f"Successfully cleaned up {count} orphaned VMs.")
        return count
    except Exception as e:
        logger.error(f"Failed to run orphaned VM cleanup: {e}")
        return 0


# ─── Zone Health API ───────────────────────────────────────────────────────

def get_zone_health() -> dict:
    """Return zone health data for admin API consumption."""
    result = {}
    with _zone_lock:
        for zone, _ in FALLBACK_ZONES:
            data = _zone_health.get(zone, {})
            cooldown_active = False
            last_fail = _zone_failures.get(zone)
            if last_fail:
                cooldown_active = (datetime.now(timezone.utc) - last_fail).total_seconds() < ZONE_COOLDOWN_SECONDS

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
