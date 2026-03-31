import os
import uuid
import logging
import asyncio
import subprocess
import signal
from typing import Optional

logger = logging.getLogger(__name__)

# --- Mode Selection ---
LOCAL_MODE = os.getenv("LOCAL_MODE", "false").lower() == "true"

# Configuration for GCP Compute Engine
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "your-gcp-project-id")
GCP_ZONE = os.getenv("GCP_ZONE", "us-central1-a")
MACHINE_TYPE = os.getenv("MACHINE_TYPE", "e2-standard-2")

# Networking Configuration
VPC_NETWORK = os.getenv("VPC_NETWORK", "agent-sandbox-vpc")
VPC_SUBNET = os.getenv("VPC_SUBNET", "agent-sandbox-subnet")

# The base image for our agent
AGENT_IMAGE_FAMILY = os.getenv("AGENT_IMAGE_FAMILY", f"projects/{GCP_PROJECT_ID}/global/images/family/blueprint-agent")

# The URL where the agent can reach this orchestrator
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8080")

# Anthropic API Key — optional fallback for Claude Code CLI.
# Primary auth is Max plan (OAuth credentials baked into the agent VM image).
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# The "Naked" Service Account with minimal GCP permissions.
AGENT_SERVICE_ACCOUNT_EMAIL = os.getenv(
    "AGENT_SERVICE_ACCOUNT_EMAIL",
    "vpm-worker@your-gcp-project-id.iam.gserviceaccount.com"
)

# Agent source directory for local mode
AGENT_DIR = os.getenv("AGENT_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "agent"))

from app.auth import create_agent_token

ARTIFACT_BUCKET_NAME = os.getenv("ARTIFACT_BUCKET_NAME", "blueprint-agent-artifacts")

async def delete_credentials_from_gcs(task_id: str):
    """Delete the temporary credential object from GCS after the agent has downloaded it."""
    object_name = f"tasks/{task_id}/.claude_credentials"

    def _delete():
        from google.cloud import storage as gcs_storage
        client = gcs_storage.Client(project=GCP_PROJECT_ID)
        bucket = client.bucket(ARTIFACT_BUCKET_NAME)
        blob = bucket.blob(object_name)
        if blob.exists():
            blob.delete()
            logger.info(f"Deleted GCS credential object: {object_name}")

    try:
        await asyncio.to_thread(_delete)
    except Exception as e:
        logger.error(f"Failed to delete GCS credential object {object_name}: {e}")


async def _upload_credentials_to_gcs(task_id: str, credentials_json: str) -> str:
    """Upload credentials to a short-lived GCS object. Returns gs:// URI."""
    from google.cloud import storage as gcs_storage
    import json

    object_name = f"tasks/{task_id}/.claude_credentials"

    def _upload():
        client = gcs_storage.Client(project=GCP_PROJECT_ID)
        bucket = client.bucket(ARTIFACT_BUCKET_NAME)
        blob = bucket.blob(object_name)
        blob.upload_from_string(data=credentials_json, content_type="application/json")
        return f"gs://{ARTIFACT_BUCKET_NAME}/{object_name}"

    return await asyncio.to_thread(_upload)


# --- Local Mode: Track subprocesses ---
_local_processes: dict[str, subprocess.Popen] = {}


async def provision_agent_vm(vm_instance_id: str, task_id: uuid.UUID, payload_uri: str = None, claude_credentials: str = None):
    """
    Provisions an agent. In LOCAL_MODE, spawns a subprocess.
    In production, creates a GCP Compute Engine VM.
    claude_credentials: JSON string of Max plan OAuth credentials to write to the VM.
    """
    if LOCAL_MODE:
        return await _provision_local(vm_instance_id, task_id, payload_uri, claude_credentials)
    return await _provision_gcp(vm_instance_id, task_id, payload_uri, claude_credentials)


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

async def _provision_local(vm_instance_id: str, task_id: uuid.UUID, payload_uri: str = None, claude_credentials: str = None):
    logger.info(f"[LOCAL] Spawning agent subprocess: {vm_instance_id} (Task: {task_id})")

    agent_jwt = create_agent_token(str(task_id), vm_instance_id)

    env = {
        **os.environ,
        "VM_INSTANCE_ID": vm_instance_id,
        "ORCHESTRATOR_URL": ORCHESTRATOR_URL,
        "AGENT_SECRET_TOKEN": agent_jwt,
        "PAYLOAD_URI": payload_uri or "",
    }
    # Max plan credentials (primary) — written to a temp file, only path passed in env
    if claude_credentials:
        import tempfile
        creds_file = tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', prefix='claude_creds_', delete=False
        )
        creds_file.write(claude_credentials)
        creds_file.close()
        os.chmod(creds_file.name, 0o600)
        env["CLAUDE_CREDENTIALS_FILE"] = creds_file.name
    # API key (fallback) — only if no Max plan credentials
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

async def _provision_gcp(vm_instance_id: str, task_id: uuid.UUID, payload_uri: str = None, claude_credentials: str = None):
    from google.cloud import compute_v1

    logger.info(f"Starting provisioning for VM: {vm_instance_id} (Task: {task_id})")

    agent_jwt = create_agent_token(str(task_id), vm_instance_id)

    # Upload credentials to GCS (never embedded in instance metadata)
    claude_creds_uri = ""
    if claude_credentials:
        from app.storage import upload_json_payload, ARTIFACT_BUCKET_NAME
        claude_creds_uri = await _upload_credentials_to_gcs(str(task_id), claude_credentials)
        logger.info(f"Uploaded Max plan credentials to GCS for task {task_id}")

    # API key fallback: passed as env var only (still in metadata, but less sensitive than OAuth tokens)
    api_key_block = ""
    if not claude_creds_uri and ANTHROPIC_API_KEY:
        api_key_block = f'ANTHROPIC_API_KEY="{ANTHROPIC_API_KEY}"'

    startup_script = f"""#!/bin/bash
    echo "Starting Agent VM Initialization..."

    # 1. Self-Destruct Mechanism (Defense in Depth)
    (sleep 3600 && echo "Maximum TTL reached. Self-terminating..." && poweroff) &

    # 2. Export environment variables needed by the agent
    cat <<EOF > /etc/agent.env
VM_INSTANCE_ID="{vm_instance_id}"
ORCHESTRATOR_URL="{ORCHESTRATOR_URL}"
AGENT_SECRET_TOKEN="{agent_jwt}"
PAYLOAD_URI="{payload_uri or ''}"
{api_key_block}
EOF

    # 3. Claude Code authentication — download from GCS, write to disk
    # GCS cleanup is handled by the orchestrator on first heartbeat (agent SA lacks delete permission)
    if [ -n "{claude_creds_uri}" ]; then
        echo "Downloading Max plan credentials from GCS..."
        mkdir -p /home/agent/.claude
        gsutil cp "{claude_creds_uri}" /home/agent/.claude/.credentials.json
        chown -R agent:agent /home/agent/.claude
        chmod 600 /home/agent/.claude/.credentials.json
        echo "Max plan credentials installed."
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
    instance_config.machine_type = f"zones/{GCP_ZONE}/machineTypes/{MACHINE_TYPE}"

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
    region = "-".join(GCP_ZONE.split("-")[:-1])
    network_interface.subnetwork = f"projects/{GCP_PROJECT_ID}/regions/{region}/subnetworks/{VPC_SUBNET}"
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
        # Delete any existing VM with this name first (stale from retries)
        try:
            instance_client.get(project=GCP_PROJECT_ID, zone=GCP_ZONE, instance=vm_instance_id)
            logger.info(f"Deleting stale VM {vm_instance_id} before re-provisioning")
            del_op = instance_client.delete(project=GCP_PROJECT_ID, zone=GCP_ZONE, instance=vm_instance_id)
            del_op.result()
        except Exception:
            pass  # VM doesn't exist — expected case
        operation = instance_client.insert(
            project=GCP_PROJECT_ID,
            zone=GCP_ZONE,
            instance_resource=instance_config
        )
        operation.result()
        return operation.name

    try:
        operation_name = await asyncio.to_thread(_insert_instance)
        logger.info(f"GCP Operation completed: {operation_name}")
        logger.info(f"Successfully provisioned VM: {vm_instance_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to provision VM {vm_instance_id}: {e}")
        return False


async def _delete_gcp(vm_instance_id: str):
    from google.cloud import compute_v1

    logger.info(f"Issuing external kill command for VM: {vm_instance_id}")

    def _delete_instance():
        instance_client = compute_v1.InstancesClient()
        operation = instance_client.delete(
            project=GCP_PROJECT_ID,
            zone=GCP_ZONE,
            instance=vm_instance_id
        )
        operation.result()
        return operation.name

    try:
        operation_name = await asyncio.to_thread(_delete_instance)
        logger.info(f"GCP Delete Operation completed: {operation_name}")
        logger.info(f"Successfully terminated VM: {vm_instance_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to terminate VM {vm_instance_id}: {e}")
        return False


async def _cleanup_gcp(active_vm_ids: list[str]):
    from google.cloud import compute_v1

    logger.info("Scanning for orphaned VMs in GCP...")

    def _list_and_delete():
        instance_client = compute_v1.InstancesClient()
        request = compute_v1.ListInstancesRequest(
            project=GCP_PROJECT_ID,
            zone=GCP_ZONE,
            filter='name:agent-vm-'
        )

        orphaned_count = 0
        for instance in instance_client.list(request=request):
            if instance.name not in active_vm_ids:
                logger.warning(f"Found orphaned VM in GCP: {instance.name}. Terminating...")
                try:
                    operation = instance_client.delete(
                        project=GCP_PROJECT_ID,
                        zone=GCP_ZONE,
                        instance=instance.name
                    )
                    operation.result()
                    orphaned_count += 1
                except Exception as e:
                    logger.error(f"Failed to delete orphaned VM {instance.name}: {e}")

        return orphaned_count

    try:
        count = await asyncio.to_thread(_list_and_delete)
        if count > 0:
            logger.info(f"Successfully cleaned up {count} orphaned VMs.")
        return count
    except Exception as e:
        logger.error(f"Failed to run orphaned VM cleanup: {e}")
        return 0
