"""Language-model layer with an offline fallback."""

from __future__ import annotations

from .base import LLMClient, LLMConfigError, LLMError
from .mock import MockLLM
from .openai_compatible import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    OpenAICompatibleClient,
    resolve_api_key,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "LLMClient",
    "LLMConfigError",
    "LLMError",
    "MockLLM",
    "OpenAICompatibleClient",
    "build_llm",
    "resolve_api_key",
]


def build_llm(provider: str = "auto", **kwargs) -> LLMClient:
    """Construct a client.

    ``auto`` uses a real provider when a key is present, otherwise falls back to
    the deterministic mock. ``mock`` always uses the mock. ``openai`` always
    requires a key and raises if one is missing.
    """

    provider = (provider or "auto").lower()
    if provider == "mock":
        return MockLLM()
    if provider in {"openai", "deepseek", "openai-compatible"}:
        return OpenAICompatibleClient(**kwargs)
    if provider == "auto":
        if resolve_api_key(kwargs.get("api_key")):
            return OpenAICompatibleClient(**kwargs)
        return MockLLM()
    raise LLMConfigError(f"未知 provider：{provider}")
