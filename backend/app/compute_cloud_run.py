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

# LLM credential env var names that must never appear as plaintext RunJobRequest
# override values. The agent resolves these from the Secret Manager manifest
# referenced by AGENT_SECRET_VERSION at startup.
_SECRET_ENV_KEYS = frozenset(
    {
        "AGENT_SECRET_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "GEMINI_API_KEY",
    }
)


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
    from app.managed_compute import (
        assert_managed_compute_enabled,
        assert_task_managed_execution_allowed_authoritative,
        ManagedComputeDisabled,
    )

    _orch_url = orchestrator_url or ORCHESTRATOR_URL

    if LOCAL_MODE:
        logger.info(
            "[CLOUD-RUN] LOCAL_MODE — skipping Job submission for task %s (vm_id=%s)",
            task_id,
            vm_id,
        )
        return True

    # This gates Cloud Run *Jobs* only.  It does not affect the Cloud Run
    # service that may be hosting Blueprint's API/control plane.
    assert_managed_compute_enabled("Cloud Run Job")

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
        # Do NOT set CLOUD_RUN_JOB here: it is a Cloud Run *reserved* env name
        # that the platform auto-populates (with the job name) on every job
        # execution. Providing it in container_overrides makes run_job fail with
        # 400 "reserved env names were provided: CLOUD_RUN_JOB". The agent
        # detects job mode from the platform-set CLOUD_RUN_JOB's presence
        # (see agent/src/index.ts), so no injection is needed.
    ]
    if payload_uri:
        env_overrides.append({"name": "PAYLOAD_URI", "value": payload_uri})
    if secret_version_name:
        # Pass the Secret Manager version URI so the agent can resolve the JWT
        # and LLM credentials from it at startup — no plaintext secret values
        # in the RunJobRequest (which persists in Cloud Audit Logs and
        # run.executions.get responses).
        env_overrides.append(
            {"name": "AGENT_SECRET_VERSION", "value": secret_version_name}
        )

    # Build non-secret LLM identifier overrides when LLM params are present.
    # Credential values (oauth_token, API keys) are intentionally excluded —
    # the agent reads them from the Secret Manager manifest via AGENT_SECRET_VERSION.
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
            # Build the combined credential key set: static denylist ∪ every key
            # the provider declared in secret_env. This is the key-based guard.
            credential_keys = _SECRET_ENV_KEYS | set(materials.secret_env.keys())
            # Build the known secret VALUE set for the fail-closed value guard.
            # Suppresses any override whose value matches a known secret, even when
            # the key is not yet in the denylist (e.g. a new provider credential).
            secret_values: set[str] = set()
            for _sv in materials.secret_env.values():
                if isinstance(_sv, str) and _sv.strip():
                    secret_values.add(_sv)
            if isinstance(oauth_token, str) and oauth_token.strip():
                secret_values.add(oauth_token)
            for k, v in llm_env.items():
                # Key guard: never pass any provider-declared or known credential
                # env var as a plaintext override.
                if k in credential_keys:
                    logger.debug(
                        "[CLOUD-RUN] Suppressing credential key %s for task %s",
                        k,
                        task_id,
                    )
                    continue
                # File-path guard: values that are file paths cannot be carried as
                # Cloud Run env overrides (the file won't exist in the container).
                if isinstance(v, str) and v.startswith("/"):
                    logger.warning(
                        "[CLOUD-RUN] Skipping file-path credential env var %s for task %s "
                        "(value starts with '/'; file-based credentials cannot be env overrides)",
                        k,
                        task_id,
                    )
                    continue
                # Value guard (fail-closed): refuse to emit any override whose value
                # matches a known secret value, even for unknown credential keys not
                # yet in the static denylist.
                if isinstance(v, str) and v in secret_values:
                    logger.warning(
                        "[CLOUD-RUN] Suppressing plaintext secret value in env var %s "
                        "for task %s (fail-closed value guard)",
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
        # ``runtime_config`` is only a per-instance cache. Re-read both durable
        # global state and the task's current project placement immediately
        # before submitting the paid execution, so either a fleet disable or a
        # local_only change received by another instance wins over a stale cache.
        await assert_task_managed_execution_allowed_authoritative(
            "Cloud Run Job", task_id
        )
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

    except ManagedComputeDisabled:
        # This is a typed intentional placement denial, not a transient Cloud
        # Run failure. The orchestrator restores the task to its queued hold;
        # converting it to False here would incorrectly fall back to GCE.
        raise
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
