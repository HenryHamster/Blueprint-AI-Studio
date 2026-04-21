from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
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
