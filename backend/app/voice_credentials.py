"""Least-privilege speech credential vending for Resident call mode.

The browser never receives provider keys and the runner never reads them from
its host environment.  This module is the server-side boundary that resolves
the two Google Secret Manager versions only for a persistent Resident
``strategic_review`` task.  The resulting values are returned only through the
exact active-call, agent-authenticated Coordinator endpoint.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from app.secrets_manager import get_user_secret


class VoiceCredentialConfigurationError(RuntimeError):
    """Call mode is requested but its server-side credential contract is absent."""


# References, rather than payloads, live in the backend deployment environment.
# They must name exact Secret Manager *versions* so a call process has a stable
# credential for its lifetime and rotations are an explicit deploy decision.
_SECRET_REF_TO_OUTPUT = {
    "CALL_DEEPGRAM_API_KEY_SECRET_VERSION": "CALL_DEEPGRAM_API_KEY",
    "CALL_CARTESIA_API_KEY_SECRET_VERSION": "CALL_CARTESIA_API_KEY",
}

# These are non-secret provider choices.  Keep the list finite: a deployment
# environment must not be able to smuggle arbitrary variables into an agent.
_REQUIRED_NON_SECRET_KEYS = (
    "CALL_DEEPGRAM_STT_MODEL",
    "CALL_CARTESIA_TTS_MODEL",
    "CALL_CARTESIA_VOICE_ID",
)
_OPTIONAL_NON_SECRET_KEYS = (
    "CALL_DEEPGRAM_STT_ENDPOINT",
    "CALL_DEEPGRAM_SAMPLE_RATE_HZ",
    "CALL_DEEPGRAM_EOT_THRESHOLD",
    "CALL_DEEPGRAM_EAGER_EOT_THRESHOLD",
    "CALL_DEEPGRAM_EOT_TIMEOUT_MS",
    "CALL_CARTESIA_TTS_ENDPOINT",
    "CALL_CARTESIA_API_VERSION",
    "CALL_CARTESIA_SAMPLE_RATE_HZ",
    "CALL_SPEECH_CONNECT_TIMEOUT_MS",
)
_MAX_CONFIG_VALUE_CHARS = 2_048
_MAX_SECRET_VALUE_CHARS = 8_192
FOREGROUND_QUALIFICATION_CONTRACT = "claude-warm-stream-json:phase0-v1"
_VOICE_CONFIGURATION_KEYS = (
    *_SECRET_REF_TO_OUTPUT,
    *_REQUIRED_NON_SECRET_KEYS,
    *_OPTIONAL_NON_SECRET_KEYS,
)


def is_resident_call_capable_task(task: Any) -> bool:
    """True only for the persistent Workday Resident task shape.

    A generic cadence ``strategic_review`` may run the same handler but is not
    a Resident and must never receive speech-provider credentials.  This guard
    is deliberately based on server-owned task fields, before any Secret
    Manager access occurs.
    """
    payload = getattr(task, "first_payload", None) or {}
    context = getattr(task, "context", None) or {}
    return (
        isinstance(payload, dict)
        and payload.get("type") == "strategic_review"
        and isinstance(context, dict)
        and context.get("_resident_session") is True
    )


def _configured_value(key: str, *, required: bool) -> str | None:
    value = os.environ.get(key, "").strip()
    if not value:
        if required:
            raise VoiceCredentialConfigurationError(
                "Resident call speech is not configured"
            )
        return None
    if len(value) > _MAX_CONFIG_VALUE_CHARS or "\x00" in value:
        raise VoiceCredentialConfigurationError(
            "Resident call speech configuration is invalid"
        )
    return value


def _secret_version_reference(key: str) -> str:
    value = _configured_value(key, required=True)
    assert value is not None
    # Do not accept ``latest``: a provider-key rotation must not silently split
    # a live call across credentials.  This is also a cheap typo guard before a
    # network request to Secret Manager.
    if not value.startswith("projects/") or "/secrets/" not in value or "/versions/" not in value:
        raise VoiceCredentialConfigurationError(
            "Resident call speech secret reference is invalid"
        )
    version = value.rsplit("/versions/", 1)[-1]
    if not version or version == "latest" or "/" in version:
        raise VoiceCredentialConfigurationError(
            "Resident call speech secret reference must name an explicit version"
        )
    return value


def validate_resident_call_deployment_configuration() -> str:
    """Fail fast on the non-secret call contract before acquiring a lease."""
    qualification = _configured_value(
        "CALL_FOREGROUND_QUALIFICATION", required=True
    )
    if qualification != FOREGROUND_QUALIFICATION_CONTRACT:
        raise VoiceCredentialConfigurationError(
            "Resident call foreground adapter is not qualification-approved"
        )
    for key in _SECRET_REF_TO_OUTPUT:
        _secret_version_reference(key)
    for key in _REQUIRED_NON_SECRET_KEYS:
        _configured_value(key, required=True)
    for key in _OPTIONAL_NON_SECRET_KEYS:
        _configured_value(key, required=False)
    return qualification


async def resolve_resident_call_speech_credentials(task: Any) -> dict[str, str]:
    """Resolve one restricted speech credential envelope for a Resident task.

    Non-Resident callers receive an empty map and never trigger Secret Manager
    access. An entirely unconfigured deployment also returns an empty map, so
    ordinary Resident/Workday operation stays available before voice is enabled.
    Once any voice setting is present, incomplete configuration or unreadable
    secrets raises, allowing the configured call path to fail closed before a
    subprocess exists. Values are intentionally never logged here or by callers.
    """
    if not is_resident_call_capable_task(task):
        return {}

    # Voice is an opt-in capability, not a prerequisite for Workday.  Treat the
    # all-absent state as disabled, but never silently downgrade a partially
    # configured deployment: that is almost certainly a broken rotation/deploy.
    if not any(os.environ.get(key, "").strip() for key in _VOICE_CONFIGURATION_KEYS):
        return {}

    refs = {
        output: _secret_version_reference(env_key)
        for env_key, output in _SECRET_REF_TO_OUTPUT.items()
    }
    non_secret: dict[str, str] = {}
    for key in _REQUIRED_NON_SECRET_KEYS:
        value = _configured_value(key, required=True)
        assert value is not None
        non_secret[key] = value
    for key in _OPTIONAL_NON_SECRET_KEYS:
        value = _configured_value(key, required=False)
        if value is not None:
            non_secret[key] = value

    fetched = await asyncio.gather(*(get_user_secret(ref) for ref in refs.values()))
    credentials: dict[str, str] = dict(non_secret)
    for output, value in zip(refs, fetched, strict=True):
        if not isinstance(value, str) or not value.strip() or len(value) > _MAX_SECRET_VALUE_CHARS:
            raise VoiceCredentialConfigurationError(
                "Resident call speech secret is unavailable"
            )
        credentials[output] = value.strip()
    return credentials
