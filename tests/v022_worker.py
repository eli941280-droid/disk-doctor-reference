"""Subprocess helpers for the v0.2.2 concurrency tests.

Kept in a plain module rather than in the test file: on Windows the ``spawn``
start method re-imports the target function's module in the child, and importing
a pytest test module there is a good way to spend an afternoon on import
errors.
"""

from __future__ import annotations

import time


def claim_plan(store_dir: str, plan_id: str, start_event, queue, hold_seconds: float = 0.0) -> None:
    """Try to take the execution claim, report the outcome on *queue*.

    Outcome tuples: ``("claimed", plan_id)``, ``("rejected", code)`` or
    ``("error", description)``.
    """

    from disk_doctor.core.clean_plan import PlanStore, PlanStoreError

    store = PlanStore(store_dir)
    if start_event is not None:
        start_event.wait(30)

    try:
        with store.claim_for_execution(plan_id) as claim:
            queue.put(("claimed", claim.plan_id))
            if hold_seconds:
                time.sleep(hold_seconds)
    except PlanStoreError as exc:
        queue.put(("rejected", exc.code))
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        queue.put(("error", f"{type(exc).__name__}: {exc}"))
