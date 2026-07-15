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

from app.auth import (
    create_agent_token,
    create_vm_scoped_token,
    AGENT_BOOTSTRAP_TOKEN_TTL_HOURS,
)
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


class _ProviderMaterials:
    """Resolved provider credentials + settings shared by the task-bound and
    taskless manifest builders. Pure data holder — no behaviour."""

    __slots__ = (
        "provider_name",
        "auth_mode_value",
        "files",
        "cli_translation",
        "secret_env",
    )

    def __init__(
        self,
        *,
        provider_name: str,
        auth_mode_value: str,
        files: list,
        cli_translation,
        secret_env: dict,
    ) -> None:
        self.provider_name = provider_name
        self.auth_mode_value = auth_mode_value
        self.files = files
        self.cli_translation = cli_translation
        self.secret_env = secret_env


def _resolve_provider_materials(
    *,
    oauth_token: str | None,
    claude_config_json: str | None,
    provider: str | None,
    auth_mode: str | None,
    model: str | None,
    provider_metadata: dict | None,
    provider_settings: dict | None,
) -> _ProviderMaterials:
    """Resolve provider impl, secret material, files, and CLI translation.

    Extracted so the task-bound (:func:`build_agent_secret_manifest`) and the
    taskless (:func:`build_taskless_agent_secret_manifest`) builders bake the
    pinned account credentials / provider / auth_mode / model identically — the
    ONLY thing that differs between the two is task binding (token scope +
    TASK_ID env + metadata.task_id), not the credential plumbing.
    """
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
    if provider_enum == LLMProviderName.ANTHROPIC:
        # Always materialize ~/.claude.json for Anthropic so the CLI doesn't
        # stall on the onboarding dialog when claude_config_json is omitted.
        # _normalize_materialized_file_content in materialize_manifest.py injects
        # hasTrustDialogAccepted into /tmp/workspace on top of whatever the
        # operator supplied. Do NOT put secrets or credentials here.
        files.append(
            FileMaterial(
                path="/home/agent/.claude.json",
                content=claude_config_json or "{}",
                mode="0600",
            )
        )
    elif claude_config_json:
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

    return _ProviderMaterials(
        provider_name=provider_name,
        auth_mode_value=auth_mode_enum.value,
        files=files,
        cli_translation=cli_translation,
        secret_env=dict(secret_material.env),
    )


def _apply_llm_env(
    env: dict,
    *,
    model: str | None,
    execution_mode: str | None,
    provider_settings: dict | None,
    materials: _ProviderMaterials,
) -> None:
    """Layer the model / execution-mode / settings / CLI-translation env on top
    of the base env, then merge secret env last (secret env wins).

    Shared by both manifest builders so the LLM env shape is identical regardless
    of task binding. Mutates ``env`` in place.
    """
    if model:
        env["LLM_MODEL"] = model
    if execution_mode:
        env["LLM_EXECUTION_MODE"] = execution_mode
    if provider_settings is not None:
        # Raw settings bag so the agent runner can consume it and, if needed,
        # re-translate server-side (CLI flag composition lives in the agent
        # so flag shape changes don't require a backend deploy).
        env["LLM_SETTINGS"] = json.dumps(provider_settings, separators=(",", ":"))
    cli_translation = materials.cli_translation
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
    env.update(materials.secret_env)


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
    materials = _resolve_provider_materials(
        oauth_token=oauth_token,
        claude_config_json=claude_config_json,
        provider=provider,
        auth_mode=auth_mode,
        model=model,
        provider_metadata=provider_metadata,
        provider_settings=provider_settings,
    )
    env = {
        "AGENT_SECRET_TOKEN": agent_jwt,
        "VM_INSTANCE_ID": vm_instance_id,
        "ORCHESTRATOR_URL": ORCHESTRATOR_URL,
        "TASK_ID": str(task_id),
        "PROVIDER": materials.provider_name,
        "AUTH_MODE": materials.auth_mode_value,
    }
    _apply_llm_env(
        env,
        model=model,
        execution_mode=execution_mode,
        provider_settings=provider_settings,
        materials=materials,
    )
    return {
        "env": env,
        "files": [
            {"path": item.path, "content": item.content, "mode": item.mode}
            for item in materials.files
        ],
        "metadata": {
            "vm_instance_id": vm_instance_id,
            "task_id": str(task_id),
            "task_type": task_type,
            "provider": materials.provider_name,
            "auth_mode": materials.auth_mode_value,
            "model": model,
            "execution_mode": execution_mode,
            "provider_settings": provider_settings or None,
        },
    }


# Poll-first boot marker. A pool VM's agent reads this env var to know it must
# boot in TASKLESS pool mode (poll /api/agent/next-role for a lease) rather than
# expecting a baked-in TASK_ID. Consumed by the agent (sibling task).
POOL_MODE_ENV_VAR = "BLUEPRINT_POOL_MODE"


def build_warm_claim_retarget_manifest(
    *,
    provider: str | None = None,
    auth_mode: str | None = None,
    model: str | None = None,
    oauth_token: str | None = None,
    claude_config_json: str | None = None,
    execution_mode: str | None = None,
    provider_metadata: dict | None = None,
    provider_settings: dict | None = None,
) -> dict:
    """Build a CREDENTIAL-ONLY secret manifest for re-targeting a warm-reused
    agent VM to a DIFFERENT LLM account/provider at warm-claim time.

    A warm VM (warm-pool member, chain VM, or backlog-reuse VM) boots with one
    account's credentials frozen into its secret manifest. When a warm claim
    runs a task whose RESOLVED account differs from that frozen boot identity,
    the agent must re-materialize the resolved account's credentials BEFORE the
    role runs — otherwise the task silently executes on the wrong account
    (rate-limit / billing misattribution, 429 cascades).

    Unlike :func:`build_agent_secret_manifest` /
    :func:`build_taskless_agent_secret_manifest`, this emits NO agent token and
    NO VM/task-identity env: the warm-claim response already rotates the JWT
    (``agent_token``) and the VM keeps its ``VM_INSTANCE_ID``. The manifest
    carries ONLY what changes on a re-target — the ``PROVIDER`` / ``AUTH_MODE``
    / ``LLM_MODEL`` env, the ``LLM_SETTINGS`` / ``LLM_CLI_TRANSLATION``
    translation, and the provider secret files. The credential plumbing is
    shared with the cold-boot builders via :func:`_resolve_provider_materials`,
    so a re-targeted VM ends up byte-identical (for the provider env + secret
    files) to a cold VM booted on the resolved account.
    """
    materials = _resolve_provider_materials(
        oauth_token=oauth_token,
        claude_config_json=claude_config_json,
        provider=provider,
        auth_mode=auth_mode,
        model=model,
        provider_metadata=provider_metadata,
        provider_settings=provider_settings,
    )
    env = {
        "PROVIDER": materials.provider_name,
        "AUTH_MODE": materials.auth_mode_value,
    }
    _apply_llm_env(
        env,
        model=model,
        execution_mode=execution_mode,
        provider_settings=provider_settings,
        materials=materials,
    )
    return {
        "env": env,
        "files": [
            {"path": item.path, "content": item.content, "mode": item.mode}
            for item in materials.files
        ],
        "metadata": {
            "provider": materials.provider_name,
            "auth_mode": materials.auth_mode_value,
            "model": model,
            "execution_mode": execution_mode,
            "provider_settings": provider_settings or None,
            # Marks the manifest as a transient re-target grant (no task/VM
            # identity) so the agent applies it as a credential swap, not a
            # fresh cold boot.
            "retarget": True,
        },
    }


def build_taskless_agent_secret_manifest(
    *,
    vm_instance_id: str,
    project_id: str,
    oauth_token: str | None = None,
    claude_config_json: str | None = None,
    provider: str | None = None,
    auth_mode: str | None = None,
    model: str | None = None,
    execution_mode: str | None = None,
    provider_metadata: dict | None = None,
    provider_settings: dict | None = None,
) -> dict:
    """Build a TASKLESS variant of the agent secret manifest for a warm-pool VM.

    Identical to :func:`build_agent_secret_manifest` in how it bakes the pinned
    account credentials / provider / auth_mode / model, but:
      - mints a VM-SCOPED token (``create_vm_scoped_token``) instead of a
        task-scoped one — the VM has not claimed any task yet;
      - omits the ``TASK_ID`` env var and ``metadata.task_id`` (there is no task);
      - sets ``BLUEPRINT_POOL_MODE=1`` so the agent boots poll-first.

    The VM-scoped token defaults to the long bootstrap TTL for the same reason a
    cold VM does: a freshly booted pool VM has not yet proven it can refresh.
    """
    agent_jwt = create_vm_scoped_token(
        vm_instance_id,
        project_id,
        ttl_hours=AGENT_BOOTSTRAP_TOKEN_TTL_HOURS,
    )
    materials = _resolve_provider_materials(
        oauth_token=oauth_token,
        claude_config_json=claude_config_json,
        provider=provider,
        auth_mode=auth_mode,
        model=model,
        provider_metadata=provider_metadata,
        provider_settings=provider_settings,
    )
    env = {
        "AGENT_SECRET_TOKEN": agent_jwt,
        "VM_INSTANCE_ID": vm_instance_id,
        "ORCHESTRATOR_URL": ORCHESTRATOR_URL,
        # No TASK_ID: this VM is taskless until it leases work.
        "PROJECT_ID": project_id,
        POOL_MODE_ENV_VAR: "1",
        "PROVIDER": materials.provider_name,
        "AUTH_MODE": materials.auth_mode_value,
    }
    _apply_llm_env(
        env,
        model=model,
        execution_mode=execution_mode,
        provider_settings=provider_settings,
        materials=materials,
    )
    return {
        "env": env,
        "files": [
            {"path": item.path, "content": item.content, "mode": item.mode}
            for item in materials.files
        ],
        "metadata": {
            "vm_instance_id": vm_instance_id,
            # No task_id — taskless pool VM.
            "project_id": project_id,
            "pool_mode": True,
            "provider": materials.provider_name,
            "auth_mode": materials.auth_mode_value,
            "model": model,
            "execution_mode": execution_mode,
            "provider_settings": provider_settings or None,
        },
    }
