"""Read-only view of the disposal backend.

v0.2.2 removes the write entry point that used to live here. ``TrashManager``
had a public ``move()`` (and a ``quarantine()`` constructor) that went straight
to the backend, bypassing the policy engine, the plan store, the approval
authority and the executor. That is a second path to the user's files, and a
security model with two paths is a security model with none.

What remains is observation only: is a recoverable backend available, and what
is it. Moving a file is done by :class:`~disk_doctor.core.safe_executor.SafeExecutor`
and by nothing else in this project.

Scope of the claim, stated precisely: this is an *application architecture*
boundary. Python has no private methods in a security sense; code running in
the same process can reach the backend object directly. What the split buys is
that no accidental call site, no registry entry and no model-authored action
can reach a write path without going through the executor's checks.
"""

from __future__ import annotations

from ..core.safe_executor import Send2TrashBackend, TrashBackend


class TrashManager:
    """Observes a trash backend. Cannot move, delete or quarantine anything."""

    def __init__(self, backend: TrashBackend | None = None) -> None:
        self._backend: TrashBackend = backend or Send2TrashBackend()

    @property
    def backend_name(self) -> str:
        return self._backend.name

    def is_available(self) -> bool:
        try:
            return bool(self._backend.is_available())
        except Exception:  # noqa: BLE001 - a broken probe means "not available"
            return False

    def backend_info(self) -> dict[str, object]:
        ok = self.is_available()
        if ok:
            reason = f"{self._backend.name} 可用"
        else:
            reason = f"{self._backend.name} 不可用，清理操作将被阻断"
        return {"backend": self._backend.name, "available": ok, "reason": reason}
