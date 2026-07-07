from __future__ import annotations

from app.models import LLMProviderName
from app.providers.anthropic import AnthropicProvider
from app.providers.base import LLMProvider
from app.providers.deepseek import DeepSeekProvider
from app.providers.gemini import GeminiProvider
from app.providers.openai import OpenAIProvider
from app.providers.zai import ZaiProvider

_PROVIDERS: dict[LLMProviderName, LLMProvider] = {
    LLMProviderName.ANTHROPIC: AnthropicProvider(),
    LLMProviderName.OPENAI: OpenAIProvider(),
    LLMProviderName.GEMINI: GeminiProvider(),
    LLMProviderName.ZAI: ZaiProvider(),
    LLMProviderName.DEEPSEEK: DeepSeekProvider(),
}


def get_provider(provider: LLMProviderName | str) -> LLMProvider:
    provider_name = (
        provider if isinstance(provider, LLMProviderName) else LLMProviderName(provider)
    )
    return _PROVIDERS[provider_name]


def list_providers() -> tuple[LLMProvider, ...]:
    return tuple(_PROVIDERS.values())


def get_all_selectable_models() -> dict[str, frozenset[str]]:
    """Return every model an operator can select, keyed by provider name string.

    Aggregates each provider's settings_schema model enum and its default
    tier->model mapping (opus/sonnet/haiku with empty runtime_config). Used by
    the pricing-coverage drift guard to detect new models missing from
    LLM_TOKEN_PRICING.
    """
    result: dict[str, frozenset[str]] = {}
    for provider_name, provider in _PROVIDERS.items():
        models: set[str] = set()
        schema = provider.settings_schema()
        model_enum = schema.get("properties", {}).get("model", {}).get("enum", [])
        for m in model_enum:
            if isinstance(m, str):
                models.add(m.lower())
        for tier in ("opus", "sonnet", "haiku"):
            m = provider.tier_to_model(tier, {})
            if m:
                models.add(m.lower())
        result[provider_name.value] = frozenset(models)
    return result
