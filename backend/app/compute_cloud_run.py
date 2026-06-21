"""Cloud Run Jobs dispatch for read-only / non-merging task types.

This module provides ``launch_agent_cloud_run_job``, a drop-in sibling to
``compute.provision_agent_vm`` for task types that never push a branch or PR.
The agent container is the same image reused verbatim; the execution environment
is Cloud Run Jobs (run-to-completion, billed per 100ms, no inbound $PORT).

The function is intentionally thin — it delegates all GCP I/O to the
``google.cloud.run_v2`` client, stamps the sentinel ``vm_machine_type`` string on
the task row (so teardown and cost attribution can identify CR-backed tasks), and
returns the same ``bool`` as ``provision_agent_vm`` so the orchestrator's calling
code is a one-line branch change.

Sentinel: tasks dispatched here get ``vm_machine_type = "cloud-run-1vcpu-2gib"``
(or ``"cloud-run-2vcpu-4gib"`` for verify_tests / smoketest which need more RAM).
All sentinels start with ``"cloud-run-"`` so callers can check with a prefix test.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

# Env-driven Cloud Run Job name (e.g. "projects/my-proj/locations/us-central1/jobs/agent")
CLOUD_RUN_JOB_NAME: str = os.getenv("CLOUD_RUN_JOB_NAME", "")

# Sentinel prefix all Cloud Run machine-type strings share.
CLOUD_RUN_MACHINE_TYPE_PREFIX = "cloud-run-"

# Sentinel vm_machine_type values stamped on CR-dispatched tasks.
# Used by cost attribution and the teardown skip in agent_status.py.
CLOUD_RUN_MACHINE_TYPE_STANDARD = "cloud-run-1vcpu-2gib"
CLOUD_RUN_MACHINE_TYPE_HEAVY = "cloud-run-2vcpu-4gib"

# Task types that need the heavier execution shape (verify_tests runs a full
# test suite; smoketest exercises the live deployment).
_HEAVY_TASK_TYPES = frozenset({"verify_tests", "smoketest", "compute_experiment"})

# taskTimeout applied to the Cloud Run Job execution (seconds).
# Read-only tasks typically finish in <30 min; 4 h matches the GCE reaper TTL
# so a stuck execution is reaped before it accumulates too much cost.
_TIMEOUT_SECONDS_STANDARD = 4 * 3600  # 4h
_TIMEOUT_SECONDS_HEAVY = 4 * 3600  # same, override per type if needed


def is_cloud_run_task(vm_machine_type: str | None) -> bool:
    """Return True when the task's vm_machine_type sentinel indicates Cloud Run."""
    return bool(
        vm_machine_type
        and str(vm_machine_type).startswith(CLOUD_RUN_MACHINE_TYPE_PREFIX)
    )


def _resolve_machine_type(task_type: str) -> str:
    return (
        CLOUD_RUN_MACHINE_TYPE_HEAVY
        if task_type in _HEAVY_TASK_TYPES
        else CLOUD_RUN_MACHINE_TYPE_STANDARD
    )


def _fetch_agent_secret_token(secret_version_name: str) -> str:
    """Fetch the agent JWT from a Secret Manager manifest version.

    Raises on any error so the caller can treat fetch failures as hard errors.
    """
    from google.cloud import secretmanager  # type: ignore[import]

    client = secretmanager.SecretManagerServiceClient()
    response = client.access_secret_version(request={"name": secret_version_name})
    manifest = json.loads(response.payload.data.decode("utf-8"))
    return manifest["env"]["AGENT_SECRET_TOKEN"]


async def launch_agent_cloud_run_job(
    vm_id: str,
    task_id: object,
    payload_uri: str | None,
    secret_version_name: str | None,
    task_type: str,
    *,
    orchestrator_url: str | None = None,
    oauth_token: str | None = None,
    provider: str | None = None,
    auth_mode: str | None = None,
    model: str | None = None,
    execution_mode: str | None = None,
    provider_metadata: dict | None = None,
    provider_settings: dict | None = None,
) -> bool:
    """Launch the agent as a Cloud Run Job execution for a read-only task.

    Mirrors the signature shape of ``compute.provision_agent_vm`` and returns
    ``True`` on success or ``False`` on any failure so the orchestrator's
    ``_provision_and_handle`` error handling works identically for both paths.

    In ``LOCAL_MODE`` (CI / local dev) the API call is skipped and ``True`` is
    returned immediately — no GCP credentials needed.

    Args:
        vm_id: Synthetic VM-ID string (already written to task.vm_instance_id).
        task_id: UUID of the task to dispatch (str-safe).
        payload_uri: GCS URI of the task payload JSON, or None.
        secret_version_name: Secret Manager version URI, or None.
        task_type: The task type string (e.g. "research").
        orchestrator_url: Override for the orchestrator base URL; defaults to
            the ``ORCHESTRATOR_URL`` env var used by the agent.
        oauth_token: OAuth token / API key for LLM provider auth.
        provider: LLM provider name (e.g. "anthropic").
        auth_mode: LLM auth mode (e.g. "api_key" or "oauth").
        model: LLM model identifier.
        execution_mode: LLM execution mode.
        provider_metadata: Provider-specific metadata dict.
        provider_settings: Provider settings bag.

    Returns:
        True when the Cloud Run Job execution was submitted successfully.
        False on any error (caller should treat as provision failure).
    """
    from app.compute import LOCAL_MODE, ORCHESTRATOR_URL

    _orch_url = orchestrator_url or ORCHESTRATOR_URL

    if LOCAL_MODE:
        logger.info(
            "[CLOUD-RUN] LOCAL_MODE — skipping Job submission for task %s (vm_id=%s)",
            task_id,
            vm_id,
        )
        return True

    job_name = CLOUD_RUN_JOB_NAME
    if not job_name:
        logger.error(
            "[CLOUD-RUN] CLOUD_RUN_JOB_NAME is not configured — cannot dispatch "
            "task %s via Cloud Run",
            task_id,
        )
        return False

    timeout_seconds = (
        _TIMEOUT_SECONDS_HEAVY
        if task_type in _HEAVY_TASK_TYPES
        else _TIMEOUT_SECONDS_STANDARD
    )

    env_overrides = [
        {"name": "VM_INSTANCE_ID", "value": str(vm_id)},
        {"name": "TASK_ID", "value": str(task_id)},
        {"name": "ORCHESTRATOR_URL", "value": _orch_url},
        {"name": "CLOUD_RUN_JOB", "value": "1"},
    ]
    if payload_uri:
        env_overrides.append({"name": "PAYLOAD_URI", "value": payload_uri})
    if secret_version_name:
        env_overrides.append(
            {"name": "AGENT_SECRET_VERSION", "value": secret_version_name}
        )
        # The agent reads AGENT_SECRET_TOKEN (raw JWT), not AGENT_SECRET_VERSION.
        # Fetch the manifest from Secret Manager and inject the token directly.
        try:
            agent_secret_token = _fetch_agent_secret_token(secret_version_name)
            env_overrides.append(
                {"name": "AGENT_SECRET_TOKEN", "value": agent_secret_token}
            )
        except Exception as exc:
            logger.error(
                "[CLOUD-RUN] Failed to fetch AGENT_SECRET_TOKEN from Secret Manager "
                "for task %s (version=%s): %s",
                task_id,
                secret_version_name,
                exc,
                exc_info=True,
            )
            return False

    # Build LLM env overrides from provider materials when LLM params are present.
    # File-based credentials (e.g. GOOGLE_APPLICATION_CREDENTIALS=/opt/...) cannot
    # be carried as env var overrides — skip any value that is a file path.
    if provider is not None or oauth_token is not None or provider_settings is not None:
        try:
            from app.compute_manifest import (
                _apply_llm_env,
                _resolve_provider_materials,
            )

            materials = _resolve_provider_materials(
                oauth_token=oauth_token,
                claude_config_json=None,
                provider=provider,
                auth_mode=auth_mode,
                model=model,
                provider_metadata=provider_metadata,
                provider_settings=provider_settings,
            )
            llm_env: dict = {
                "PROVIDER": materials.provider_name,
                "AUTH_MODE": materials.auth_mode_value,
            }
            _apply_llm_env(
                llm_env,
                model=model,
                execution_mode=execution_mode,
                provider_settings=provider_settings,
                materials=materials,
            )
            for k, v in llm_env.items():
                # Skip file-path credential values — Cloud Run env overrides
                # cannot carry file contents and the file won't exist in the container.
                if isinstance(v, str) and v.startswith("/"):
                    logger.warning(
                        "[CLOUD-RUN] Skipping file-path credential env var %s for task %s "
                        "(value starts with '/'; file-based credentials cannot be env overrides)",
                        k,
                        task_id,
                    )
                    continue
                env_overrides.append({"name": k, "value": str(v)})
        except Exception as exc:
            logger.error(
                "[CLOUD-RUN] Failed to build LLM env overrides for task %s: %s",
                task_id,
                exc,
                exc_info=True,
            )
            return False

    try:
        from google.cloud import run_v2  # type: ignore[import]

        client = run_v2.JobsClient()

        overrides = run_v2.RunJobRequest.Overrides(
            task_count=1,
            timeout=_build_duration(timeout_seconds),
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(
                    env=[
                        run_v2.EnvVar(name=e["name"], value=e["value"])
                        for e in env_overrides
                    ]
                )
            ],
        )

        request = run_v2.RunJobRequest(name=job_name, overrides=overrides)
        operation = client.run_job(request=request)
        logger.info(
            "[CLOUD-RUN] Submitted Job execution for task %s (vm_id=%s, "
            "operation=%s, timeout=%ds)",
            task_id,
            vm_id,
            operation.operation.name if hasattr(operation, "operation") else "unknown",
            timeout_seconds,
        )
        return True

    except Exception as exc:
        logger.error(
            "[CLOUD-RUN] Failed to submit Job execution for task %s (vm_id=%s): %s",
            task_id,
            vm_id,
            exc,
            exc_info=True,
        )
        return False


def _build_duration(seconds: int):
    """Build a protobuf Duration for the Cloud Run API."""
    try:
        from google.protobuf import duration_pb2  # type: ignore[import]

        return duration_pb2.Duration(seconds=seconds)
    except ImportError as exc:
        raise ImportError(
            "_build_duration: google.protobuf is not installed; "
            "cannot set Cloud Run job timeout override"
        ) from exc
