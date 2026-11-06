"""Tool abstraction.

A tool is a named capability with a declared input shape. Declaring the shape is
not ceremony: it is what lets the registry be inspected, documented, and (later)
exposed to an LLM without hand-writing a schema per call site.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    """Static description of a capability."""

    name: str
    summary: str
    inputs: dict[str, str] = field(default_factory=dict)
    read_only: bool = True
    danger: str = "none"

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "summary": self.summary,
            "inputs": dict(self.inputs),
            "read_only": self.read_only,
            "danger": self.danger,
        }


@dataclass
class Tool:
    """A spec bound to a callable."""

    spec: ToolSpec
    handler: Callable[..., Any]

    @property
    def name(self) -> str:
        return self.spec.name

    def __call__(self, **kwargs: Any) -> Any:
        return self.handler(**kwargs)
