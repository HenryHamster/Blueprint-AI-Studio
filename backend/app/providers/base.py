from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.models import LLMAuthMode, LLMProviderName


@dataclass(frozen=True)
class ValidationResult:
    secret: str
    provider_metadata: dict[str, Any]


@dataclass(frozen=True)
class FileMaterial:
    path: str
    content: str
    mode: str = "0600"


@dataclass(frozen=True)
class SecretMaterial:
    env: dict[str, str]
    files: tuple[FileMaterial, ...] = ()


@dataclass(frozen=True)
class CLITranslation:
    """Output of settings_to_cli(): what the agent runner should apply to a CLI invocation.

    - `env`: extra environment variables to set for the subprocess (merged with secret env).
    - `args`: ordered CLI flags to append after the base invocation.
    - `settings_files`: files to materialize on disk before invocation (e.g., ~/.gemini/settings.json).
    - `prompt_fragments`: strings to prepend/append to the system prompt as fallback for knobs
      the CLI cannot express natively (advisor mode, tool_choice on Codex, etc).
    - `unsupported`: knob names from the settings bag that produced no native translation.
      Logged by the runner; operators use this to know when a setting is being silently dropped.
    """

    env: dict[str, str] = field(default_factory=dict)
    args: tuple[str, ...] = ()
    settings_files: tuple[FileMaterial, ...] = ()
    prompt_fragments: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()


class LLMProvider(ABC):
    name: LLMProviderName
    supported_auth_modes: tuple[LLMAuthMode, ...]

    @abstractmethod
    def validate_secret(
        self,
        raw: str,
        auth_mode: LLMAuthMode,
        provider_metadata: Mapping[str, Any] | None = None,
    ) -> ValidationResult:
        raise NotImplementedError

    @abstractmethod
    def tier_to_model(self, tier: str, runtime_config: Mapping[str, Any]) -> str | None:
        raise NotImplementedError

    @abstractmethod
    def rate_limit_patterns(self, auth_mode: LLMAuthMode) -> tuple[str, ...]:
        raise NotImplementedError

    @abstractmethod
    def required_metadata_fields(self, auth_mode: LLMAuthMode) -> tuple[str, ...]:
        raise NotImplementedError

    def supported_execution_modes(self) -> tuple[str, ...]:
        return ("default",)

    @abstractmethod
    def secret_material(
        self,
        secret: str | None,
        auth_mode: LLMAuthMode,
        provider_metadata: Mapping[str, Any] | None = None,
    ) -> SecretMaterial:
        raise NotImplementedError

    def settings_schema(self) -> dict[str, Any]:
        """JSON-schema-shaped declaration of this provider's tunable knobs.

        Consumed by the admin API to drive dynamic UI rendering, and by
        validate_settings() to reject unknown keys / enum violations.
        Default: empty schema (provider exposes no knobs).
        """
        return {"type": "object", "properties": {}, "additionalProperties": False}

    def validate_settings(self, bag: Mapping[str, Any]) -> dict[str, Any]:
        """Validate a settings bag against settings_schema().

        Returns a cleaned copy with only recognized keys and coerced types.
        Raises ValueError on enum violations or structural mismatch. Unknown
        top-level keys are rejected (fail-closed) so typos don't silently no-op.
        """
        return _validate_against_schema(bag or {}, self.settings_schema(), path="")

    def settings_to_cli(self, bag: Mapping[str, Any]) -> CLITranslation:
        """Translate a validated settings bag into CLI env/args/files/fallbacks.

        Default implementation returns an empty translation. Providers override
        to map their specific knobs onto their CLI's surface.
        """
        return CLITranslation()

    def account_has_credentials(self, account: Mapping[str, Any] | Any) -> bool:
        secret_blob_ref = _account_value(account, "secret_blob_ref")
        oauth_token = _account_value(account, "oauth_token")
        runtime_secret = _account_value(account, "runtime_secret")
        return bool(runtime_secret or secret_blob_ref or oauth_token)

    def ensure_supported_auth_mode(self, auth_mode: LLMAuthMode) -> None:
        if auth_mode not in self.supported_auth_modes:
            allowed = ", ".join(mode.value for mode in self.supported_auth_modes)
            raise ValueError(f"{self.name.value} does not support auth_mode={auth_mode.value!r}; allowed: {allowed}")

    def normalize_metadata(self, provider_metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        metadata = dict(provider_metadata or {})
        missing = [field for field in self.required_metadata_fields(self.supported_auth_modes[0]) if field not in metadata]
        if missing:
            raise ValueError(f"Missing provider metadata fields: {', '.join(missing)}")
        return metadata


def validate_required_metadata_fields(
    provider: LLMProvider,
    auth_mode: LLMAuthMode,
    provider_metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    metadata = dict(provider_metadata or {})
    missing = [field for field in provider.required_metadata_fields(auth_mode) if not metadata.get(field)]
    if missing:
        raise ValueError(f"Missing provider metadata fields: {', '.join(missing)}")
    return metadata


def _account_value(account: Mapping[str, Any] | Any, key: str) -> Any:
    if isinstance(account, Mapping):
        return account.get(key)
    return getattr(account, key, None)


def parse_json_secret(raw: str, *, require_object: bool = True) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("secret must be valid JSON") from exc
    if require_object and not isinstance(parsed, dict):
        raise ValueError("secret JSON must be an object")
    return parsed


def tier_model_from_runtime_config(
    provider: LLMProviderName,
    tier: str,
    runtime_config: Mapping[str, Any],
    defaults: Mapping[str, str],
) -> str | None:
    provider_map = runtime_config.get("provider_tier_model_map", {}) if isinstance(runtime_config, Mapping) else {}
    configured = provider_map.get(provider.value, {}) if isinstance(provider_map, Mapping) else {}
    if isinstance(configured, Mapping) and configured.get(tier):
        return configured[tier]
    return defaults.get(tier)


# ---------------------------------------------------------------------------
# Lightweight schema validator for provider settings bags.
#
# Supports: type (object/string/integer/number/boolean/array), enum, properties,
# additionalProperties (bool), items (for arrays), default. Not a full JSON
# Schema implementation — just enough to validate typed bags without a dep.
# Raises ValueError with a dotted path on any violation. Unknown keys are
# rejected when additionalProperties is False (default).
# ---------------------------------------------------------------------------
def _validate_against_schema(value: Any, schema: Mapping[str, Any], *, path: str) -> Any:
    if not isinstance(schema, Mapping):
        return value
    expected = schema.get("type")
    if expected == "object":
        if value is None:
            value = {}
        if not isinstance(value, Mapping):
            raise ValueError(f"{path or '<root>'}: expected object, got {type(value).__name__}")
        props = schema.get("properties") or {}
        extra_ok = schema.get("additionalProperties", False)
        required = schema.get("required") or ()
        cleaned: dict[str, Any] = {}
        for key, raw in dict(value).items():
            if key in props:
                sub_path = f"{path}.{key}" if path else key
                cleaned[key] = _validate_against_schema(raw, props[key], path=sub_path)
            elif extra_ok:
                cleaned[key] = raw
            else:
                raise ValueError(f"{path or '<root>'}: unknown key {key!r}")
        missing = [key for key in required if key not in cleaned]
        if missing:
            raise ValueError(f"{path or '<root>'}: missing required keys: {', '.join(missing)}")
        return cleaned
    if expected == "string":
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{path}: expected string")
        enum = schema.get("enum")
        if enum is not None and value not in enum:
            raise ValueError(f"{path}: value {value!r} not in allowed values {list(enum)}")
        return value
    if expected == "integer":
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{path}: expected integer")
        return value
    if expected == "number":
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path}: expected number")
        return value
    if expected == "boolean":
        if value is None:
            return None
        if not isinstance(value, bool):
            raise ValueError(f"{path}: expected boolean")
        return value
    if expected == "array":
        if value is None:
            return None
        if not isinstance(value, list):
            raise ValueError(f"{path}: expected array")
        items = schema.get("items")
        if items is None:
            return list(value)
        return [
            _validate_against_schema(item, items, path=f"{path}[{i}]")
            for i, item in enumerate(value)
        ]
    return value


def merge_settings(*bags: Mapping[str, Any] | None) -> dict[str, Any]:
    """Deep-merge a sequence of settings bags (later bags win).

    Used to compose provider_settings_defaults → task_type_provider_overrides →
    payload.provider_settings. Nested objects are merged recursively; scalars,
    arrays, and None values from later bags replace earlier ones wholesale.
    """
    result: dict[str, Any] = {}
    for bag in bags:
        if not bag:
            continue
        _deep_merge_into(result, bag)
    return result


def _deep_merge_into(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            _deep_merge_into(existing, value)
        else:
            target[key] = value
