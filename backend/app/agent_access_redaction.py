"""Credential-field classification for bounded coding-agent projections."""

from __future__ import annotations

import re
from typing import Any

_SECRET_FIELD_NAMES = frozenset(
    {
        "access_key",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "bearer",
        "client_secret",
        "container_registry_auth",
        "cookie",
        "credentials",
        "env",
        "environment",
        "headers",
        "key",
        "jwt",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "signed_url",
        "token",
        "upload_url",
        "download_url",
    }
)
_SECRET_FIELD_PARTS = frozenset(
    {
        "authorization",
        "bearer",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "jwt",
        "password",
        "passwords",
        "secret",
        "secrets",
    }
)
_NON_SECRET_TOKEN_FIELDS = frozenset(
    {
        "cached_tokens",
        "completion_tokens",
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "reasoning_tokens",
        "total_tokens",
    }
)
_NON_SECRET_KEY_FIELDS = frozenset(
    {
        "dedupe_key",
        "dedupe_keys",
        "idempotency_key",
        "idempotency_keys",
        "logical_key",
        "logical_keys",
    }
)
_CREDENTIAL_KEY_QUALIFIERS = frozenset(
    {"access", "api", "client", "encryption", "private", "secret", "signing"}
)


def is_secret_field(key: Any) -> bool:
    """Return whether a nested coding-agent field is credential-bearing."""
    raw_key = str(key)
    # Normalize both separator-delimited and camel/Pascal-case keys before
    # matching. Case-folding first would collapse ``accessToken`` to
    # ``accesstoken`` and let common SDK-shaped credential fields pass through.
    snake_key = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", raw_key)
    snake_key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", snake_key)
    normalized = re.sub(r"[^a-z0-9]+", "_", snake_key.casefold()).strip("_")
    if normalized in _SECRET_FIELD_NAMES:
        return True
    parts = {part for part in normalized.split("_") if part}
    if parts & _SECRET_FIELD_PARTS:
        return True
    # Do not treat accounting fields such as input_tokens/token_count as
    # secrets; only conventional credential-bearing *_token/*_key names.
    if normalized.endswith(("_token", "_tokens")):
        return normalized not in _NON_SECRET_TOKEN_FIELDS
    if normalized.endswith(("_key", "_keys")):
        return normalized not in _NON_SECRET_KEY_FIELDS
    if parts & {"key", "keys"} and parts & _CREDENTIAL_KEY_QUALIFIERS:
        return True
    if "auth" in parts and (
        len(parts) == 1
        or bool(
            parts
            & {
                "credential",
                "credentials",
                "header",
                "headers",
                "key",
                "keys",
                "token",
                "tokens",
                "value",
            }
        )
    ):
        return True
    return normalized.endswith("_auth")
