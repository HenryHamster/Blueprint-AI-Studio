"""Pure helpers for building the agent VM secret manifest.

Extracted from compute.py to keep the manifest-building logic separable from
the GCP/local provisioner machinery. No mutable module-level state lives here;
everything is a pure transformation over its inputs and the env-derived
constants below.
"""

import json
import os
import re
import uuid

from app.auth import create_agent_token, AGENT_BOOTSTRAP_TOKEN_TTL_HOURS
from app.models import LLMAuthMode, LLMProviderName
from app.providers.base import FileMaterial
from app.providers.registry import get_provider


ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8080")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


_PAYLOAD_URI_RE = re.compile(r"^gs://[a-zA-Z0-9._-]+/[a-zA-Z0-9._/-]+$")


def validate_payload_uri(payload_uri: str) -> None:
    """Raise ValueError if payload_uri is not a safe GCS URI."""
    if not _PAYLOAD_URI_RE.match(payload_uri):
        raise ValueError(
            f"Invalid payload_uri: must match gs://<bucket>/<object> with "
            f"alphanumeric, dots, dashes, underscores, and slashes only. Got: {payload_uri!r}"
        )


def build_agent_secret_manifest(
    *,
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
    project_id: str = "",
) -> dict:
    """Build the provider-aware secret manifest consumed by agent startup."""
    # This is the cold-start INITIAL grant: the VM has not yet proven it can
    # refresh, so it receives the long bootstrap TTL to survive a deploy /
    # rebake cutover that straddles token expiry. The agent is rotated back
    # down to the short TTL the moment it calls /api/agent/refresh-token
    # (see agent_refresh_token) or is warm-handed-off on a VM already proven
    # refresh-capable (see _finalize_warm_claim).
    agent_jwt = create_agent_token(
        str(task_id),
        vm_instance_id,
        task_type=task_type,
        project_id=project_id,
        ttl_hours=AGENT_BOOTSTRAP_TOKEN_TTL_HOURS,
    )
    provider_name = provider or LLMProviderName.ANTHROPIC.value
    provider_enum = LLMProviderName(provider_name)
    resolved_auth_mode = auth_mode or (
        LLMAuthMode.OAUTH.value
        if provider_name == LLMProviderName.ANTHROPIC.value and oauth_token
        else LLMAuthMode.API_KEY.value
    )
    auth_mode_enum = LLMAuthMode(resolved_auth_mode)
    provider_impl = get_provider(provider_enum)
    runtime_secret = oauth_token
    if (
        runtime_secret is None
        and provider_enum == LLMProviderName.ANTHROPIC
        and auth_mode_enum == LLMAuthMode.API_KEY
    ):
        runtime_secret = ANTHROPIC_API_KEY or None
    secret_material = provider_impl.secret_material(
        runtime_secret,
        auth_mode_enum,
        provider_metadata=provider_metadata,
    )
    files = list(secret_material.files)
    if claude_config_json:
        files.append(
            FileMaterial(
                path="/home/agent/.claude.json", content=claude_config_json, mode="0600"
            )
        )

    # Translate the resolved provider settings bag into CLI env/args/files/
    # fallbacks. The agent runner consumes LLM_SETTINGS to apply these to
    # the subprocess invocation. settings_files are materialized alongside
    # secret files so providers like Gemini can pre-write a settings.json
    # with a custom alias before invocation.
    # Translate the bag even when empty so an explicit `{}` still produces an
    # LLM_CLI_TRANSLATION env var; that signals to the agent that the backend
    # speaks the settings-bag protocol. Legacy env-var fallback is reserved
    # for backends that don't populate provider_settings at all (None).
    #
    # Inject the resolved `model` argument into the bag before translation
    # when the bag didn't already carry one. Compatibility gates (OpenAI
    # service_tier=flex, Gemini preview-access) key on the effective model,
    # and resolve_task_provider_settings only populates `bag["model"]` when
    # a layer explicitly set it — tier-mapped defaults take the separate
    # `model` arg path. Without this merge, those gates would see None on
    # the common "no explicit model, tier-mapped fallback" code path and
    # fail closed on every invocation.
    cli_translation = None
    if provider_settings is not None:
        effective_bag: dict = dict(provider_settings)
        if model and not effective_bag.get("model"):
            effective_bag["model"] = model
        cli_translation = provider_impl.settings_to_cli(effective_bag, auth_mode_enum)
        files.extend(cli_translation.settings_files)

    env = {
        "AGENT_SECRET_TOKEN": agent_jwt,
        "VM_INSTANCE_ID": vm_instance_id,
        "ORCHESTRATOR_URL": ORCHESTRATOR_URL,
        "TASK_ID": str(task_id),
        "PROVIDER": provider_name,
        "AUTH_MODE": auth_mode_enum.value,
    }
    if model:
        env["LLM_MODEL"] = model
    if execution_mode:
        env["LLM_EXECUTION_MODE"] = execution_mode
    if provider_settings is not None:
        # Raw settings bag so the agent runner can consume it and, if needed,
        # re-translate server-side (CLI flag composition lives in the agent
        # so flag shape changes don't require a backend deploy).
        env["LLM_SETTINGS"] = json.dumps(provider_settings, separators=(",", ":"))
    if cli_translation is not None:
        # Pre-translated CLI shape so a naive runner can apply without
        # re-implementing the translation layer. Agent code may still prefer
        # to translate from LLM_SETTINGS for forward-compat.
        env["LLM_CLI_TRANSLATION"] = json.dumps(
            {
                "env": cli_translation.env,
                "args": list(cli_translation.args),
                "prompt_fragments": list(cli_translation.prompt_fragments),
                "unsupported": list(cli_translation.unsupported),
            },
            separators=(",", ":"),
        )
        # Merge translator-provided env on top of secret env so settings can't
        # overwrite auth keys. secret_material.env takes precedence.
        for k, v in cli_translation.env.items():
            env.setdefault(k, v)
    env.update(secret_material.env)
    return {
        "env": env,
        "files": [
            {"path": item.path, "content": item.content, "mode": item.mode}
            for item in files
        ],
        "metadata": {
            "vm_instance_id": vm_instance_id,
            "task_id": str(task_id),
            "task_type": task_type,
            "provider": provider_name,
            "auth_mode": auth_mode_enum.value,
            "model": model,
            "execution_mode": execution_mode,
            "provider_settings": provider_settings or None,
        },
    }
