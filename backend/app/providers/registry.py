from __future__ import annotations

from app.models import LLMProviderName
from app.providers.anthropic import AnthropicProvider
from app.providers.base import LLMProvider
from app.providers.gemini import GeminiProvider
from app.providers.openai import OpenAIProvider

_PROVIDERS: dict[LLMProviderName, LLMProvider] = {
    LLMProviderName.ANTHROPIC: AnthropicProvider(),
    LLMProviderName.OPENAI: OpenAIProvider(),
    LLMProviderName.GEMINI: GeminiProvider(),
}


def get_provider(provider: LLMProviderName | str) -> LLMProvider:
    provider_name = provider if isinstance(provider, LLMProviderName) else LLMProviderName(provider)
    return _PROVIDERS[provider_name]


def list_providers() -> tuple[LLMProvider, ...]:
    return tuple(_PROVIDERS.values())

