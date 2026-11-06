"""The contract between the language model and the agent.

The model gets exactly one job: map a sentence onto a closed set of intents.
It never receives a filesystem handle, never produces a file list, and its
answer is *advisory* — the rule router makes the actual routing decision.

The Pydantic model uses ``extra="forbid"`` so an over-eager model that tries to
smuggle in ``{"paths": [...]}`` or ``{"command": "..."}`` gets a validation
error instead of a surprise.
"""

from __future__ import annotations

import enum
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..core.schemas import Intent

__all__ = [
    "IntentType",
    "LLMContractError",
    "LLMIntentResult",
    "parse_llm_intent",
    "to_internal_intent",
]


class IntentType(str, enum.Enum):
    """Closed set. Adding a member is a code change, not a prompt change."""

    DIAGNOSE = "diagnose"
    SCAN = "scan"
    PLAN = "plan"
    PREVIEW = "preview"
    HELP = "help"
    UNKNOWN = "unknown"


class LLMContractError(ValueError):
    """The model returned something outside the contract."""


class LLMIntentResult(BaseModel):
    """Strict shape for a model response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: IntentType
    scope_id: str | None = None
    rationale: str = Field(default="", max_length=200)


_MAP: dict[IntentType, Intent] = {
    IntentType.DIAGNOSE: Intent.DIAGNOSE,
    IntentType.SCAN: Intent.DIAGNOSE,
    IntentType.PLAN: Intent.CLEAN_QUICK,
    IntentType.PREVIEW: Intent.CLEAN_QUICK,
    IntentType.HELP: Intent.UNKNOWN,
    IntentType.UNKNOWN: Intent.UNKNOWN,
}


def parse_llm_intent(payload: str | dict[str, Any]) -> LLMIntentResult:
    """Validate a model response. Raises :class:`LLMContractError` on breach."""

    if isinstance(payload, str):
        text = payload.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMContractError(f"响应不是合法 JSON：{exc}") from exc
    else:
        data = payload

    if not isinstance(data, dict):
        raise LLMContractError("响应必须是 JSON 对象")

    try:
        return LLMIntentResult.model_validate(data)
    except ValidationError as exc:
        raise LLMContractError(f"响应不符合契约：{exc.errors()}") from exc


def to_internal_intent(result: LLMIntentResult) -> Intent:
    """Advisory mapping only. The router still decides what actually runs."""

    return _MAP[result.intent]
