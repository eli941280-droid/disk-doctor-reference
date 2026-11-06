"""Session state.

Two rules drive the design:

1. **State is external, not remembered.** Anything needed later lives in a
   typed object, so a long conversation cannot "forget" a veto.
2. **A confirmation belongs to one plan.** ``confirmed_plan_ids`` is a set of
   plan ids, never a boolean. Re-generating a plan therefore invalidates every
   previous approval automatically — the single most common cause of
   over-deletion in agent tools.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..core.schemas import CleanPlan


@dataclass(frozen=True)
class Event:
    ts: datetime
    kind: str
    detail: str = ""


@dataclass
class SessionState:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    slots: dict[str, str] = field(default_factory=dict)
    vetoes: dict[str, str] = field(default_factory=dict)
    plans: dict[str, CleanPlan] = field(default_factory=dict)
    confirmed_plan_ids: set[str] = field(default_factory=set)
    history: list[Event] = field(default_factory=list)


class ContextManager:
    """Small, explicit state container for one session."""

    def __init__(self) -> None:
        self.state = SessionState()

    # -- slots ------------------------------------------------------------- #

    def set_slot(self, key: str, value: str) -> None:
        self.state.slots[key] = value

    def get_slot(self, key: str, default: str | None = None) -> str | None:
        return self.state.slots.get(key, default)

    # -- vetos ------------------------------------------------------------- #

    def record_veto(self, key: str, reason: str = "") -> None:
        self.state.vetoes[key] = reason or "用户否决"

    def is_vetoed(self, key: str) -> bool:
        return key in self.state.vetoes

    def vetoed_keys(self) -> frozenset[str]:
        return frozenset(self.state.vetoes)

    # -- plans & confirmations --------------------------------------------- #

    def store_plan(self, plan: CleanPlan) -> None:
        self.state.plans[plan.plan_id] = plan

    def get_plan(self, plan_id: str) -> CleanPlan | None:
        return self.state.plans.get(plan_id)

    def mark_confirmed(self, plan_id: str) -> None:
        """Record approval for exactly one plan.

        Older approvals are dropped. Keeping them would allow a recycled plan
        (identical content, re-generated later) to be executed on a stale
        approval, which is the failure mode this whole class exists to prevent.
        """

        self.state.confirmed_plan_ids.clear()
        self.state.confirmed_plan_ids.add(plan_id)

    def is_confirmed(self, plan_id: str) -> bool:
        return plan_id in self.state.confirmed_plan_ids

    def clear_confirmations(self) -> None:
        self.state.confirmed_plan_ids.clear()

    def invalidate(self, plan_id: str) -> None:
        self.state.confirmed_plan_ids.discard(plan_id)

    # -- history ----------------------------------------------------------- #

    def log(self, kind: str, detail: str = "") -> None:
        self.state.history.append(
            Event(ts=datetime.now(UTC), kind=kind, detail=detail)
        )

    def snapshot(self) -> dict:
        return {
            "session_id": self.state.session_id,
            "slots": dict(self.state.slots),
            "vetoes": dict(self.state.vetoes),
            "plans": sorted(self.state.plans),
            "confirmed": sorted(self.state.confirmed_plan_ids),
            "events": len(self.state.history),
        }
