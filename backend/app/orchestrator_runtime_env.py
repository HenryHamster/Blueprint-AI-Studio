"""Env-backed runtime-credential resolution extracted from ``app.orchestrator``.

This sibling module hosts the small, self-contained cluster that resolves
env-backed runtime credentials (Anthropic / OpenAI / Gemini keys, Codex OAuth
from ``CODEX_HOME``, Gemini service-account JSON plus project / location) for
the no-pool-account fallback, along with the per-provider credential-hint
string and the per-provider default-model map. These helpers are pure: they
read only environment variables and the filesystem and return a tuple.

Re-exports from ``app.orchestrator`` keep existing callers
(``from app.orchestrator import _resolve_env_runtime_fallback``, etc.) working.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from app.models import Task

logger = logging.getLogger("blueprint")


_ENV_FALLBACK_DEFAULT_MODELS = {
    "anthropic": "claude-opus-5",
    "openai": "gpt-5.4",
    "gemini": "gemini-2.5-pro",
    "zai": "glm-5.2",
    "deepseek": "deepseek-v4-flash",
}


def _provider_env_credential_hint(provider: str | None) -> str:
    if provider == "openai":
        return "OPENAI_API_KEY or CODEX_API_KEY or CODEX_HOME/auth.json"
    if provider == "gemini":
        return "GEMINI_API_KEY or GOOGLE_APPLICATION_CREDENTIALS (+ GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION)"
    if provider == "zai":
        return "ZAI_API_KEY or ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN with ANTHROPIC_BASE_URL"
    if provider == "deepseek":
        return "DEEPSEEK_API_KEY or ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN with ANTHROPIC_BASE_URL"
    return "ANTHROPIC_API_KEY"


def _resolve_env_runtime_fallback(
    *,
    provider: str | None,
    model: str | None,
    execution_mode: str | None,
    task: Task,
    runtime_config: dict[str, Any],
) -> tuple[str | None, str, str | None, str | None, str | None, dict[str, str]]:
    """Return env-backed runtime credentials when no pool account is available.

    `execution_mode` is no longer resolved from runtime config (retired with
    the settings-bag redesign); whatever the caller passes is round-tripped.
    """
    provider_name = provider or "anthropic"
    runtime_model = model or _ENV_FALLBACK_DEFAULT_MODELS.get(provider_name)
    runtime_execution_mode = execution_mode
    provider_metadata: dict[str, str] = {}

    if provider_name == "openai":
        secret = os.getenv("OPENAI_API_KEY") or os.getenv("CODEX_API_KEY")
        auth_mode = "api_key"
        if not secret:
            codex_home = (os.getenv("CODEX_HOME") or "").strip()
            auth_path = Path(codex_home) / "auth.json" if codex_home else None
            if not auth_path or not auth_path.exists():
                return (
                    None,
                    provider_name,
                    None,
                    runtime_model,
                    runtime_execution_mode,
                    provider_metadata,
                )
            try:
                secret = auth_path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning(
                    f"[ORCHESTRATOR] Failed to read OpenAI auth.json from CODEX_HOME={codex_home!r}: {exc}"
                )
                return (
                    None,
                    provider_name,
                    None,
                    runtime_model,
                    runtime_execution_mode,
                    provider_metadata,
                )
            auth_mode = "oauth"
        return (
            secret,
            provider_name,
            auth_mode,
            runtime_model,
            runtime_execution_mode,
            provider_metadata,
        )

    if provider_name == "gemini":
        secret = os.getenv("GEMINI_API_KEY")
        if secret:
            return (
                secret,
                provider_name,
                "api_key",
                runtime_model,
                runtime_execution_mode,
                provider_metadata,
            )
        service_account_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if not service_account_path:
            return (
                None,
                provider_name,
                None,
                runtime_model,
                runtime_execution_mode,
                provider_metadata,
            )
        try:
            secret = Path(service_account_path).read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                f"[ORCHESTRATOR] Failed to read GOOGLE_APPLICATION_CREDENTIALS={service_account_path!r}: {exc}"
            )
            return (
                None,
                provider_name,
                None,
                runtime_model,
                runtime_execution_mode,
                provider_metadata,
            )
        project = (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
        location = (os.getenv("GOOGLE_CLOUD_LOCATION") or "").strip()
        if not project or not location:
            logger.warning(
                "[ORCHESTRATOR] GOOGLE_APPLICATION_CREDENTIALS is set for Gemini service-account fallback "
                "but GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION are both required"
            )
            return (
                None,
                provider_name,
                None,
                runtime_model,
                runtime_execution_mode,
                provider_metadata,
            )
        if project:
            provider_metadata["project"] = project
        if location:
            provider_metadata["location"] = location
        return (
            secret,
            provider_name,
            "service_account",
            runtime_model,
            runtime_execution_mode,
            provider_metadata,
        )

    if provider_name == "zai":
        base_url = (
            os.getenv("ZAI_ANTHROPIC_BASE_URL")
            or os.getenv("ZAI_BASE_URL")
            or os.getenv("ANTHROPIC_BASE_URL")
            or ""
        ).strip()
        provider_secret = os.getenv("ZAI_API_KEY")
        compatible_secret = (
            os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY")
        ) if base_url else None
        secret = provider_secret or compatible_secret
        if base_url:
            provider_metadata["anthropic_base_url"] = base_url
        if not secret:
            return (
                None,
                provider_name,
                None,
                runtime_model,
                runtime_execution_mode,
                provider_metadata,
            )
        return (
            secret,
            provider_name,
            "api_key",
            runtime_model,
            runtime_execution_mode,
            provider_metadata,
        )

    if provider_name == "deepseek":
        base_url = (
            os.getenv("DEEPSEEK_ANTHROPIC_BASE_URL")
            or os.getenv("DEEPSEEK_BASE_URL")
            or os.getenv("ANTHROPIC_BASE_URL")
            or ""
        ).strip()
        provider_secret = os.getenv("DEEPSEEK_API_KEY")
        compatible_secret = (
            os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY")
        ) if base_url else None
        secret = provider_secret or compatible_secret
        if base_url:
            provider_metadata["anthropic_base_url"] = base_url
        if not secret:
            return (
                None,
                provider_name,
                None,
                runtime_model,
                runtime_execution_mode,
                provider_metadata,
            )
        return (
            secret,
            provider_name,
            "api_key",
            runtime_model,
            runtime_execution_mode,
            provider_metadata,
        )

    secret = os.getenv("ANTHROPIC_API_KEY")
    if not secret:
        return (
            None,
            provider_name,
            None,
            runtime_model,
            runtime_execution_mode,
            provider_metadata,
        )
    return (
        secret,
        provider_name,
        "api_key",
        runtime_model,
        runtime_execution_mode,
        provider_metadata,
    )
