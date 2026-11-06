"""Tool registry.

Kept intentionally small: register, look up, list, invoke with error wrapping.
The registry is the only place that knows the full set of capabilities, which is
what makes ``disk-doctor tools`` a truthful answer to "what can this thing do?".

v0.2.2 makes the boundary explicit: this registry is **model-facing**, so it
refuses to hold a write tool. ``SafeExecutor`` is therefore absent from it — the
only way to run a plan is ``DiskDoctorAgent.execute_plan``, which requires a
stored plan id and a one-time approval receipt. A registry that could expose
"move these files" to a language model would undo every other gate in the
project, because the model would no longer need to pass through them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from .base import Tool, ToolSpec


class ToolNotFound(KeyError):
    pass


class ToolRegistry:
    """A model-facing registry. Write tools are rejected at registration."""

    def __init__(self, *, llm_facing: bool = True) -> None:
        self.llm_facing = llm_facing
        self._tools: dict[str, Tool] = {}

    def register(
        self,
        name: str,
        summary: str,
        handler: Callable[..., Any],
        *,
        inputs: dict[str, str] | None = None,
        read_only: bool = True,
        danger: str = "none",
    ) -> Tool:
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        if self.llm_facing and not read_only:
            raise ValueError(
                f"LLM-facing registry may not expose a write tool: {name}. "
                "User files are moved only by SafeExecutor, via agent.execute_plan."
            )
        tool = Tool(
            spec=ToolSpec(
                name=name,
                summary=summary,
                inputs=inputs or {},
                read_only=read_only,
                danger=danger,
            ),
            handler=handler,
        )
        self._tools[name] = tool
        return tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFound(name) from exc

    def call(self, name: str, **kwargs: Any) -> Any:
        return self.get(name)(**kwargs)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self) -> list[dict[str, Any]]:
        return [self._tools[n].spec.describe() for n in self.names()]

    def llm_tools(self) -> list[dict[str, Any]]:
        """Exactly what a model is allowed to see: the read-only subset."""

        return [t.spec.describe() for t in self._tools.values() if t.spec.read_only]

    def write_tools(self) -> list[str]:
        return [n for n, t in self._tools.items() if not t.spec.read_only]

    def read_only_tools(self) -> Iterable[Tool]:
        return (t for t in self._tools.values() if t.spec.read_only)

    def assert_read_only(self) -> None:
        """Fail loudly if anything writable slipped in."""

        writers = self.write_tools()
        if writers:
            raise AssertionError(f"registry exposes write tools: {sorted(writers)}")

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools
