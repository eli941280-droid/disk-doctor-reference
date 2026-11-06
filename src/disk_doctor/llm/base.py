"""LLM abstraction.

The interface is intentionally one method. Everything the agent does that must be
reliable — routing, policy, planning, execution, verification — is plain Python.
The model is used to phrase things for humans, and when no key is configured a
deterministic substitute takes over, labelled as such in the output.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMError(RuntimeError):
    """Any failure originating from the language-model layer."""


class LLMConfigError(LLMError):
    """Missing or invalid configuration (key, base URL, model name)."""


class LLMClient(ABC):
    """Minimal text-completion interface."""

    name: str = "base"
    model: str = ""

    @abstractmethod
    def complete(self, system: str, user: str, *, temperature: float | None = None) -> str:
        """Return a single assistant message."""

    def describe(self) -> dict[str, str]:
        return {"provider": self.name, "model": self.model}
