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
        "creds",
        "credentials",
        "database_dsn",
        "dotenv",
        "dsn",
        "env",
        "env_vars",
        "environment",
        "environment_variables",
        "headers",
        "key",
        "jwt",
        "passphrase",
        "passwd",
        "password",
        "private_key",
        "pwd",
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
        "creds",
        "credential",
        "credentials",
        "jwt",
        "passphrase",
        "passphrases",
        "passwd",
        "password",
        "passwords",
        "pwd",
        "secret",
        "secrets",
    }
)
_AUTH_CONTAINER_PARTS = frozenset(
    {
        "config",
        "configuration",
        "credential",
        "credentials",
        "data",
        "header",
        "headers",
        "key",
        "keys",
        "list",
        "map",
        "options",
        "params",
        "settings",
        "token",
        "tokens",
        "value",
        "values",
    }
)
_AUTH_ROOT_PARTS = frozenset(
    {"auth", "authentication", "oauth", "oauth2", "oidc"}
)
_ENV_CONTAINER_PARTS = frozenset(
    {
        "config",
        "data",
        "file",
        "files",
        "list",
        "map",
        "options",
        "settings",
        "value",
        "values",
        "var",
        "vars",
        "variable",
        "variables",
    }
)
_HEADER_METADATA_PARTS = frozenset({"count", "counts", "name", "names"})
_LOCATION_FIELD_PARTS = frozenset({"dsn", "uri", "uris", "url", "urls"})
_SENSITIVE_LOCATION_QUALIFIERS = frozenset(
    {
        "connection",
        "database",
        "db",
        "download",
        "mongo",
        "mongodb",
        "presigned",
        "proxy",
        "redis",
        "repository",
        "signed",
        "upload",
        "webhook",
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
_NON_SECRET_TOKEN_METADATA_PARTS = frozenset(
    {
        "accepted",
        "accumulated",
        "audio",
        "available",
        "billable",
        "budget",
        "budgets",
        "capacity",
        "completion",
        "consumed",
        "count",
        "counts",
        "generated",
        "hour",
        "hours",
        "input",
        "limit",
        "limits",
        "max",
        "maximum",
        "min",
        "minimum",
        "minute",
        "minutes",
        "output",
        "per",
        "prompt",
        "prediction",
        "rate",
        "reasoning",
        "reported",
        "rejected",
        "remaining",
        "second",
        "seconds",
        "throughput",
        "total",
        "usage",
        "used",
        "window",
    }
)
_CREDENTIAL_TOKEN_QUALIFIERS = frozenset(
    {"access", "api", "auth", "bearer", "client", "oauth", "refresh", "session"}
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
    {
        "access",
        "account",
        "api",
        "aws",
        "client",
        "encryption",
        "gcp",
        "google",
        "master",
        "material",
        "private",
        "registry",
        "secret",
        "service",
        "shared",
        "signing",
        "ssh",
        "storage",
        "subscription",
    }
)
_URI_USERINFO_RE = re.compile(
    r"(?i)(?P<scheme>\b[a-z][a-z0-9+.-]*://)[^/\s@]+@"
)
_DOUBLE_QUOTED_FIELD_RE = re.compile(
    r'(?P<prefix>"(?P<key>[A-Za-z][A-Za-z0-9_.-]{0,95})"\s*:\s*")'
    r'(?P<value>(?:\\.|[^"\\])*)(?P<suffix>")'
)
_SINGLE_QUOTED_FIELD_RE = re.compile(
    r"(?P<prefix>'(?P<key>[A-Za-z][A-Za-z0-9_.-]{0,95})'\s*:\s*')"
    r"(?P<value>(?:\\.|[^'\\])*)(?P<suffix>')"
)
_BASIC_AUTH_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:proxy[- ]?)?authorization\s*[:=]\s*basic)"
    r"\s+[A-Za-z0-9+/_=-]+"
)
_API_KEY_HEADER_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:x[-_])?api[-_]?key\s*[:=]\s*)[^\s,;]+"
)


def redact_uri_userinfo(value: str) -> str:
    """Redact password-bearing or token-only userinfo in any URI scheme."""
    return _URI_USERINFO_RE.sub(r"\g<scheme>***@", value)


def _redact_quoted_secret_field(match: re.Match[str]) -> str:
    if not is_secret_field(match.group("key")):
        return match.group(0)
    return f'{match.group("prefix")}[redacted]{match.group("suffix")}'


def redact_agent_text(value: str) -> str:
    """Redact credential shapes embedded inside otherwise diagnostic text."""
    value = redact_uri_userinfo(value)
    value = _DOUBLE_QUOTED_FIELD_RE.sub(_redact_quoted_secret_field, value)
    value = _SINGLE_QUOTED_FIELD_RE.sub(_redact_quoted_secret_field, value)
    value = _BASIC_AUTH_RE.sub(r"\g<prefix> [redacted]", value)
    return _API_KEY_HEADER_RE.sub(r"\g<prefix>[redacted]", value)


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
    if parts & {"env", "environment"} and parts & _ENV_CONTAINER_PARTS:
        return True
    auth_parts = parts & _AUTH_ROOT_PARTS
    if auth_parts and (len(parts) == 1 or parts & _AUTH_CONTAINER_PARTS):
        return True
    header_parts = parts & {"header", "headers"}
    if header_parts and not parts & _HEADER_METADATA_PARTS:
        return True
    if parts & _LOCATION_FIELD_PARTS and parts & _SENSITIVE_LOCATION_QUALIFIERS:
        return True
    if "connection" in parts and parts & {"string", "strings"}:
        return True
    # Do not treat accounting fields such as input_tokens/token_count as
    # secrets; only conventional credential-bearing *_token/*_key names.
    if parts & {"token", "tokens"}:
        if parts & _CREDENTIAL_TOKEN_QUALIFIERS:
            return True
        if normalized in _NON_SECRET_TOKEN_FIELDS:
            return False
        # Generic wrappers such as tokenValue/tokenList are credential-shaped
        # even when the secret qualifier is omitted.
        return not bool(parts & _NON_SECRET_TOKEN_METADATA_PARTS)
    if normalized in _NON_SECRET_KEY_FIELDS:
        return False
    if parts & {"key", "keys"} and parts & _CREDENTIAL_KEY_QUALIFIERS:
        return True
    return normalized.endswith("_auth")
