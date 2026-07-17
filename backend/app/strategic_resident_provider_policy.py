"""Fail-closed provider routing for bounded Strategic Resident turns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from app.models import LLMProviderName

_RESIDENT_TASK_TYPE = "strategic_resident_turn"
_KNOWN_PROVIDERS = frozenset(provider.value for provider in LLMProviderName)
# Only Claude-compatible CLIs currently enforce the complete --disallowedTools
# denylist. Codex and Gemini otherwise expose ambient shell/workspace authority.
_NO_TOOL_RESIDENT_PROVIDERS = frozenset({"anthropic", "zai", "deepseek"})


class ResidentProviderPolicyHold(ValueError):
    """The frozen policy cannot be satisfied without widening authority."""


@dataclass(frozen=True)
class ResidentProviderRouting:
    """Ordered provider constraints derived from a frozen Resident Task."""

    primary_provider: str | None
    primary_model: str | None
    provider_preference: tuple[str, ...]
    allow_fallback: bool


def validate_resident_model_provider(
    routing: ResidentProviderRouting | None,
    infer_provider: Callable[[str], str | None],
) -> None:
    """Reject a pinned model that belongs to a different known provider."""
    if routing and routing.primary_provider and routing.primary_model:
        model_provider = infer_provider(routing.primary_model)
        if model_provider and model_provider != routing.primary_provider:
            raise ResidentProviderPolicyHold(
                "Resident model is incompatible with its pinned provider"
            )


def _first_payload(task: Any) -> Mapping[str, Any]:
    payloads = getattr(task, "payloads", None) or []
    return payloads[0] if payloads and isinstance(payloads[0], Mapping) else {}


def _provider_name(raw: Any) -> str | None:
    if isinstance(raw, LLMProviderName):
        return raw.value
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _provider_list(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(
        dict.fromkeys(
            provider
            for provider in (_provider_name(item) for item in raw)
            if provider in _KNOWN_PROVIDERS
        )
    )


def resolve_resident_provider_routing(
    task: Any,
    *,
    project_provider_preference: Sequence[str],
    allow_cross_provider_fallback: bool,
) -> ResidentProviderRouting | None:
    """Resolve a Resident snapshot against current project routing policy.

    ``None`` means the Task is not a Resident turn. A Resident policy that is
    absent, malformed, or forbidden raises :class:`ResidentProviderPolicyHold`
    so callers keep the Task queued instead of silently widening its route.
    """

    payload = _first_payload(task)
    if payload.get("type") != _RESIDENT_TASK_TYPE:
        return None
    context = getattr(task, "context", None) or {}
    if context.get("_resident_no_silent_provider_downgrade") is not True:
        raise ResidentProviderPolicyHold(
            "Resident turn is missing its no-silent-provider-downgrade fence"
        )

    provider_policy = payload.get("resident_provider_policy")
    failover_policy = payload.get("resident_failover_policy")
    provider_policy = provider_policy if isinstance(provider_policy, Mapping) else {}
    failover_policy = failover_policy if isinstance(failover_policy, Mapping) else {}
    primary_provider = _provider_name(provider_policy.get("provider"))
    primary_model_raw = provider_policy.get("model")
    primary_model = (
        primary_model_raw.strip()
        if isinstance(primary_model_raw, str) and primary_model_raw.strip()
        else None
    )
    if primary_provider is None and primary_model is not None:
        raise ResidentProviderPolicyHold(
            "Resident provider policy cannot pin a model without its provider"
        )
    project_providers = tuple(
        provider
        for provider in dict.fromkeys(project_provider_preference)
        if provider in _NO_TOOL_RESIDENT_PROVIDERS
    )
    if primary_provider is None:
        if not project_providers:
            raise ResidentProviderPolicyHold(
                "Resident project routing has no provider with enforced no-tool mode"
            )
        return ResidentProviderRouting(
            primary_provider=None,
            primary_model=None,
            provider_preference=project_providers,
            allow_fallback=allow_cross_provider_fallback,
        )
    if primary_provider not in _KNOWN_PROVIDERS:
        raise ResidentProviderPolicyHold(
            f"Resident primary provider {primary_provider!r} is not supported"
        )
    if primary_provider not in _NO_TOOL_RESIDENT_PROVIDERS:
        raise ResidentProviderPolicyHold(
            "Resident primary provider cannot enforce the no-tool execution boundary"
        )
    if primary_provider not in project_providers:
        raise ResidentProviderPolicyHold(
            "Resident primary provider is forbidden by project routing policy"
        )

    compatible = _provider_list(failover_policy.get("compatible_providers"))
    allowed_failovers = (
        tuple(
            provider
            for provider in compatible
            if provider != primary_provider and provider in project_providers
        )
        if allow_cross_provider_fallback
        else ()
    )
    return ResidentProviderRouting(
        primary_provider=primary_provider,
        primary_model=primary_model,
        provider_preference=(primary_provider, *allowed_failovers),
        allow_fallback=bool(allowed_failovers),
    )


__all__ = [
    "ResidentProviderPolicyHold",
    "ResidentProviderRouting",
    "resolve_resident_provider_routing",
    "validate_resident_model_provider",
]
