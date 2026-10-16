from __future__ import annotations

from typing import Any

from app.ai.base import AIProvider, ProviderUnavailable
from app.ai.providers.anthropic import AnthropicProvider
from app.ai.providers.openai_compat import OpenAICompatProvider
from app.config import get_settings

XAI_MODELS = ["grok-4-latest", "grok-3", "grok-3-mini", "grok-2-latest"]
OPENAI_MODELS = ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "o4-mini"]
ANTHROPIC_MODELS = [
    "claude-sonnet-4-20250514",
    "claude-3-7-sonnet-latest",
    "claude-3-5-sonnet-latest",
    "claude-3-5-haiku-latest",
]


def get_provider(name: str) -> AIProvider:
    settings = get_settings()
    key = name.lower()
    if key == "xai":
        return OpenAICompatProvider("xai", settings.xai_api_key, settings.xai_api_base, XAI_MODELS)
    if key == "openai":
        return OpenAICompatProvider("openai", settings.openai_api_key, settings.openai_api_base, OPENAI_MODELS)
    if key == "anthropic":
        return AnthropicProvider(settings.anthropic_api_key, settings.anthropic_api_base, ANTHROPIC_MODELS)
    raise ProviderUnavailable(f"Unknown AI provider: {name}")


def list_provider_status() -> list[dict[str, Any]]:
    rows = []
    for name, models in (("xai", XAI_MODELS), ("openai", OPENAI_MODELS), ("anthropic", ANTHROPIC_MODELS)):
        provider = get_provider(name)
        rows.append(
            {
                "name": name,
                "configured": provider.configured(),
                "models": models,
                "status": "ready" if provider.configured() else "disabled",
            }
        )
    return rows
